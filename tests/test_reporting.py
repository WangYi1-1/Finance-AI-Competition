"""报告保留口径、精确金额与缺数状态，证据可以按文件和页码定位。"""
import csv
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from finance_agent.analysis import analyze, compare_metrics
from finance_agent.reporting import generate_report, period_label
from finance_agent.schemas import AnalysisResult, Evidence, ExtractedReport, Metric, RunResult


def metric(value="12952608201.89", *, start="2023-01-01", end="2023-09-30", basis="ytd"):
    return Metric(
        name="revenue", company="名称:含冒号的测试公司", value=Decimal(value),
        raw_value=value, unit="元", period_start=date.fromisoformat(start),
        period_end=date.fromisoformat(end), basis=basis, scope="consolidated",
        evidence_ids=["doc:p3:table0"],
    )


def run_result(tmp_path, metrics, comparisons):
    evidence = Evidence(
        id="doc:p3:table0", document_id="doc", document_name="测试公司_第三季度报告.pdf",
        pdf_page=3, kind="table", text="营业收入 12,952,608,201.89",
    )
    report = ExtractedReport(
        document_id="doc", document_name=evidence.document_name, company=metrics[0].company,
        report_type="q3", period_end=date(2023, 9, 30), metrics=metrics,
    )
    return RunResult(
        run_id="report-test", mode="demo", model="offline", reports=[report],
        analysis=AnalysisResult(comparisons=comparisons), evidence={evidence.id: evidence},
        run_dir=str(tmp_path),
    )


def test_same_end_date_quarter_and_cumulative_are_distinct_and_missing_stays_missing(tmp_path):
    cumulative = metric()
    quarter = metric("4000000000.01", start="2023-07-01", basis="quarter")
    comparisons = [compare_metrics(current, None, "yoy") for current in (cumulative, quarter)]
    result = run_result(tmp_path, [cumulative, quarter], comparisons)
    before = result.model_dump_json()

    generate_report(result)
    text = (tmp_path / "report.md").read_text(encoding="utf-8")
    comparison_text = text.split("## 同比与环比", 1)[1].split("## 异常线索", 1)[0]
    headings = [line for line in comparison_text.splitlines() if line.startswith("### ")]
    assert len(headings) == 2
    assert "2023年1—9月累计" in headings[0]
    assert "2023年第三季度（7—9月）" in headings[1]
    assert "合并报表" in headings[0]
    assert comparison_text.count("状态：缺少可比数据；不提供增长率。") == 2
    assert "缺少可比金额不等于该期间金额为零" in comparison_text
    assert "129.53 亿元" in comparison_text
    assert "40.00 亿元" in comparison_text
    assert "2022" in comparison_text and "报告" in comparison_text
    assert result.model_dump_json() == before


def test_source_links_use_filename_and_pdf_page_with_existing_anchors(tmp_path):
    current = metric()
    result = run_result(tmp_path, [current], [compare_metrics(current, None, "yoy")])
    generate_report(result)
    text = (tmp_path / "report.md").read_text(encoding="utf-8")

    assert "[测试公司_第三季度报告.pdf · PDF第3页（表格）](#evidence-1)" in text
    assert '<a id="evidence-1"></a>' in text
    assert "### 测试公司_第三季度报告.pdf · PDF第3页（表格）" in text
    assert "<details><summary>查看证据编号</summary>" in text
    assert "`doc:p3:table0`" in text
    assert "doc:p3:table0" not in text.split("## 原文证据", 1)[0]


def test_small_nonzero_amounts_explain_rounding_while_csv_and_json_keep_precision(tmp_path):
    current = metric("120.25")
    base = metric("100.00", start="2022-01-01", end="2022-09-30")
    comparison = compare_metrics(current, base, "yoy")
    result = run_result(tmp_path, [current, base], [comparison])
    generate_report(result)
    text = (tmp_path / "report.md").read_text(encoding="utf-8")

    assert "状态：已计算；20.25%。" in text
    assert "本期金额：≈0.00 亿元" in text and "精确值 120.25 元" in text
    assert "基期金额（2022年1—9月累计）：≈0.00 亿元" in text and "精确值 100.00 元" in text
    assert "增减金额：≈0.00 亿元" in text and "精确值 20.25 元" in text
    assert comparison.explanation in text
    assert "年初至今累计/合并报表" in text
    assert "ytd/consolidated" not in text
    assert "已核验" in text
    with (tmp_path / "metrics.csv").open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert rows[0]["basis"] == "ytd"
    assert rows[0]["scope"] == "consolidated"
    assert rows[0]["status"] == "verified"
    assert rows[0]["value_yuan"] == "120.25"
    assert rows[0]["evidence_ids"] == "doc:p3:table0"
    saved = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert saved["reports"][0]["metrics"][0]["value"] == "120.25"
    assert saved["analysis"]["comparisons"][0]["delta"] == "20.25"


def test_point_and_legacy_comparison_periods_are_explicit():
    assert period_label(date(2023, 9, 30), date(2023, 9, 30), "point") == "2023-09-30期末时点"
    assert period_label(None, date(2023, 9, 30), None) == "截至2023-09-30（期间信息未记录）"


def test_actual_public_report_uses_billions_human_details_and_earnings_transition(tmp_path):
    root = Path(__file__).resolve().parents[1]
    pointer = json.loads((root / "samples/public/latest_verified_run.json").read_text(encoding="utf-8"))
    result = RunResult.model_validate_json((root / pointer["result_path"]).read_text(encoding="utf-8"))
    result.run_dir = str(tmp_path)
    result.analysis = analyze(result.reports)
    before = result.model_dump_json()

    generate_report(result)
    text = (tmp_path / "report.md").read_text(encoding="utf-8")
    comparison_text = text.split("## 同比与环比", 1)[1].split("## 异常线索", 1)[0]
    tcl_loss = [part for part in comparison_text.split("### ")
                if "TCL科技" in part and "合并净利润" in part and "2024年1—9月累计" in part]
    assert len(tcl_loss) == 1
    assert "状态：不适用（由盈转亏）；不提供增长率。" in tcl_loss[0]
    assert "-18.29 亿元" in tcl_loss[0] and "55.68 亿元" in tcl_loss[0]
    assert "-73.97 亿元" in tcl_loss[0]
    assert "-132.85%" not in tcl_loss[0]
    signal_text = text.split("## 异常线索", 1)[1].split("## 核验范围和待处理事项", 1)[0]
    assert "|项目|内容|" in signal_text
    assert "|归母净利润|15.25 亿元|" in signal_text
    assert "金额核对允许误差" in signal_text
    assert "<details><summary>查看原始计算明细（金额为元）</summary>" in signal_text
    assert result.model_dump_json() == before
    with (tmp_path / "metrics.csv").open(encoding="utf-8-sig", newline="") as stream:
        csv_values = [row["value_yuan"] for row in csv.DictReader(stream)]
    assert csv_values == [str(metric.value) for report in result.reports for metric in report.metrics]
