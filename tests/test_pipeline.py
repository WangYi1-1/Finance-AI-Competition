"""演示的端到端验收：文件、金额、证据与运行记录保持一致。"""
import csv
import json
import pytest
from decimal import Decimal
from pathlib import Path

from finance_agent.demo import ensure_demo_files
from finance_agent.demo import extract_demo_report
from finance_agent.llm import ModelError
from finance_agent.pipeline import run_analysis
from finance_agent.schemas import RunResult


def test_demo_writes_auditable_outputs_without_any_model_call(tmp_path, monkeypatch):
    def unexpected_model(*args, **kwargs):
        raise AssertionError("演示模式不能构建模型客户端或调用接口")

    monkeypatch.setattr("finance_agent.pipeline.DeepSeekClient", unexpected_model)
    result = run_analysis(ensure_demo_files(), mode="demo", output_root=tmp_path)
    assert result.mode == "demo"
    assert result.usage["model_calls"] == 0
    assert len(result.reports) == 2
    assert len(result.analysis.signals) == 6
    assert all(len(report.metrics) == 9 for report in result.reports)
    assert all(metric.status == "verified" for report in result.reports for metric in report.metrics)

    latest = next(report for report in result.reports if report.period_end.year == 2025)
    metrics = {metric.name: metric for metric in latest.metrics}
    assert metrics["revenue"].value == Decimal("1200000000.00")
    assert metrics["operating_cashflow"].value == Decimal("-20000000.00")
    revenue_yoy = next(item for item in result.analysis.comparisons
                       if item.metric == "revenue" and item.kind == "yoy" and item.period_end.year == 2025)
    assert revenue_yoy.percent == Decimal("20")

    folder = Path(result.run_dir)
    assert folder.is_relative_to(tmp_path)
    assert all((folder / name).is_file() for name in ["report.md", "result.json", "metrics.csv", "trace.jsonl"])
    reloaded = RunResult.model_validate_json((folder / "result.json").read_text(encoding="utf-8"))
    assert reloaded == result

    with (folder / "metrics.csv").open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 18
    cash_row = next(row for row in rows if row["metric"] == "经营现金流净额" and row["period_end"] == "2025-12-31")
    assert Decimal(cash_row["value_yuan"]) == Decimal("-20000000.00")

    report_text = (folder / "report.md").read_text(encoding="utf-8")
    assert "虚构演示，不调用模型" in report_text
    used = {identifier for report in result.reports for metric in report.metrics for identifier in metric.evidence_ids}
    used |= {identifier for signal in result.analysis.signals for identifier in signal.evidence_ids}
    used |= {identifier for comparison in result.analysis.comparisons for identifier in comparison.evidence_ids}
    assert used <= result.evidence.keys()
    for identifier in used:
        source = result.evidence[identifier]
        assert source.pdf_page >= 1
        assert f"`{identifier}`" in report_text
        assert source.document_name in report_text
        assert f"PDF第{source.pdf_page}页" in report_text
    assert len(list((folder / "sources").glob("*.pdf"))) == 2

    events = [json.loads(line) for line in (folder / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(event["tool"] == "financial_analysis" for event in events)
    assert not any(event["tool"] in ("model_request", "model_response") for event in events)


def test_duplicate_pdf_is_not_counted_as_another_report(tmp_path):
    path = ensure_demo_files()[0]
    result = run_analysis([path, path], mode="demo", output_root=tmp_path)
    assert len(result.reports) == 1
    assert len(result.analysis.signals) == 3
    assert any("重复文件" in warning for warning in result.warnings)


def test_unreadable_input_does_not_silently_expand_result_coverage(tmp_path):
    bad_path = tmp_path / "invalid.pdf"
    bad_path.write_bytes(b"This is not a PDF")
    result = run_analysis([ensure_demo_files()[0], bad_path], mode="demo", output_root=tmp_path / "runs")
    assert len(result.reports) == 1
    assert any("invalid.pdf" in warning and "不完整" in warning for warning in result.warnings)
    report_text = (Path(result.run_dir) / "report.md").read_text(encoding="utf-8")
    assert "invalid.pdf" in report_text


def test_model_failure_keeps_completed_reports_and_marks_partial_coverage(tmp_path, monkeypatch):
    class StubClient:
        returned_models = {"test-model"}
        usage = {"model_calls": 2}

        def __init__(self, *args, **kwargs):
            pass

    calls = []

    def extract(doc, client, audit, progress=None):
        calls.append(doc.id)
        if len(calls) == 2:
            raise ModelError("DeepSeek 暂时繁忙，请稍后重试。")
        return extract_demo_report(doc)

    monkeypatch.setattr("finance_agent.pipeline.DeepSeekClient", StubClient)
    monkeypatch.setattr("finance_agent.pipeline.extract_metrics", extract)
    result = run_analysis(ensure_demo_files(), mode="live", output_root=tmp_path)
    assert len(result.reports) == 1
    assert any("覆盖范围不完整" in w for w in result.warnings)
    assert (Path(result.run_dir) / "result.json").is_file()
    events = [json.loads(x) for x in (Path(result.run_dir) / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(e["tool"] == "document_failed" and e["output"]["reason"] == "model_error" for e in events)


def test_model_failure_before_any_completed_report_still_raises(tmp_path, monkeypatch):
    monkeypatch.setattr("finance_agent.pipeline.DeepSeekClient", lambda *args: object())

    def fail(*args, **kwargs):
        raise ModelError("DeepSeek 身份验证失败，请检查本地密钥和账户权限。")

    monkeypatch.setattr("finance_agent.pipeline.extract_metrics", fail)
    with pytest.raises(ModelError, match="身份验证失败"):
        run_analysis(ensure_demo_files()[:1], mode="live", output_root=tmp_path)
