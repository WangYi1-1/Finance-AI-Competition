"""受控流水线：解析→提取→校验补查→程序分析→证据报告。"""
from __future__ import annotations

import hashlib
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .analysis import analyze
from .audit import AuditLog
from .comparatives import supplement_comparatives
from .config import ROOT, Settings
from .demo import extract_demo_report
from .extraction import extract_metrics
from .enrichment import enrich_report
from .provenance import runtime_provenance, source_record, seal_run
from .llm import DeepSeekClient, ModelError
from .parser import parse_financial_doc
from .reporting import generate_report
from .schemas import RunResult


def run_analysis(paths: list[Path], mode: str = "live", output_root: Path = ROOT / "output",
                 progress=None) -> RunResult:
    if mode not in ("live", "demo"):
        raise ValueError("运行模式只能是live或demo。")
    if not 1 <= len(paths) <= 6:
        raise ValueError("请提供1至6份PDF，推荐同一公司多个可比期间。")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8]
    run_dir = (Path(output_root) / run_id).resolve()
    run_dir.mkdir(parents=True)
    audit = AuditLog(run_dir / "trace.jsonl")
    try:
        settings = Settings.load() if mode == 'live' else Settings()
    except (OSError, ValueError) as exc:
        audit.record('configuration_failed', outputs={'exception_type':type(exc).__name__,
                     'reason':'本地配置无法读取或数字参数不合法；未记录配置内容。'}, status='failed')
        raise ValueError('本地配置无法读取，请检查超时、调用次数、输出长度和temperature数字设置。') from None
    audit.secrets.append(settings.api_key)
    provenance = runtime_provenance(mode)
    audit.record("run_start", inputs={"mode": mode, "model": settings.model if mode == 'live' else "demo-rule-reader",
                                      "file_count": len(paths), "prompt_sha256": hashlib.sha256(
                                      (ROOT / "prompts" / "extract.txt").read_bytes()).hexdigest()})
    audit.record("runtime_provenance", outputs=provenance)
    from .tools import tool_catalog
    audit.record("tool_catalog", outputs=tool_catalog())
    if mode == 'live':
        audit.record("configuration_access", inputs={"file": ".env", "values_logged": False})
    try:
        client = DeepSeekClient(settings, audit) if mode == "live" else None
    except ModelError as exc:
        audit.exception("client_setup_failure", exc)
        raise
    evidence = {}
    reports = []
    warnings = []
    seen = set()

    def notify(text):
        if progress:
            progress(text)

    for path in paths:
        path = Path(path)
        try:
            notify(f"正在解析 {path.name}，保留页码与表格位置。")
            audit.record("file_read_requested", inputs={"file": path.name})
            doc = parse_financial_doc(path)
            if doc.id in seen:
                warnings.append(f"{path.name}与已输入文件内容相同，已跳过重复文件。")
                continue
            seen.add(doc.id)
            audit.record("file_access", inputs={"file": path.name}, outputs={"sha256": doc.sha256,
                         "document_id": doc.id, "pages": doc.page_count})
            source_dir = run_dir / "sources"
            source_dir.mkdir(exist_ok=True)
            shutil.copyfile(path, source_dir / f"{doc.id}.pdf")
            provenance["sources"].append(source_record(doc, source_dir / f"{doc.id}.pdf"))
            audit.record("parse_financial_doc", outputs=doc.model_dump(mode="json"))
            evidence.update({b.id: b for b in doc.blocks})
            warnings.extend(f"{doc.name}：{x}" for x in doc.warnings)
            notify(f"正在提取 {doc.name} 的财务指标和附注证据。")
            report = extract_metrics(doc, client, audit, progress=notify) if client else extract_demo_report(doc)
            audit.record("extract_metrics", inputs={"mode": mode, "document_id": doc.id}, outputs=report.model_dump(mode="json"))
            notify(f"正在核对 {doc.name} 原表中的可比期间金额。")
            original_count = len(report.metrics)
            report = supplement_comparatives(report, doc)
            report = enrich_report(report, doc)
            audit.record("enrich_financial_details", inputs={"document_id": doc.id},
                         outputs={"reasons": len(report.performance_reasons), "items": len(report.financial_details),
                                  "checks": report.detail_checks})
            audit.record("supplement_comparatives", inputs={"document_id": doc.id},
                         outputs={"added_metric_count": len(report.metrics) - original_count,
                                  "report": report.model_dump(mode="json")})
            reports.append(report)
        except ModelError as exc:
            audit.exception("document_exception", exc, context={"document": path.name})
            audit.record("document_failed", outputs={"document": path.name, "reason": "model_error"}, status="failed")
            if not reports:
                audit.record("run_failed", outputs={"reason": "model_error"}, status="failed")
                raise
            warnings.append(f"{path.name}的模型调用未完成，已停止后续提取；本次仅分析已完成的{len(reports)}份报告，覆盖范围不完整。")
            break
        except Exception as exc:
            audit.exception("document_exception", exc, context={"document": path.name})
            warnings.append(f"{path.name}未能完成可核验提取，已排除；结果覆盖范围不完整。")
            audit.record("document_failed", outputs={"document": path.name, "reason": "parse_or_validation_failed"}, status="failed")
    if not reports:
        raise ValueError("所有文件都未能完成可核验提取。请检查PDF是否为文字版，或先运行虚构演示。")
    if not any(m.status == "verified" for report in reports for m in report.metrics):
        raise ValueError("模型返回的数据未通过原文行列或单位核验，没有可用于计算的指标。请检查本次日志或更换清晰报告。")
    notify("正在校验比较口径、计算同比环比，并识别三类异常线索。")
    analysis = analyze(reports)
    audit.record("financial_analysis", inputs={"document_ids": [r.document_id for r in reports]},
                 outputs=analysis.model_dump(mode="json"))
    result = RunResult(run_id=run_id, mode=mode,
                       model=", ".join(sorted(client.returned_models)) if client else "demo-rule-reader",
                       reports=reports, analysis=analysis, evidence=evidence, run_dir=str(run_dir),
                       warnings=warnings, usage=client.usage if client else {"model_calls": 0}, provenance=provenance)
    notify("正在生成指标表、可追溯报告和执行日志。")
    generate_report(result)
    audit.record("generate_report", outputs={"files": ["report.md", "metrics.csv", "result.json", "trace.jsonl"]})
    seal_run(result, audit)
    return result
