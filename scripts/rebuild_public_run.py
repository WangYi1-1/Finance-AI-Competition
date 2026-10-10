"""对保存的真实模型输出应用当前核验规则，合并公开样本；不调用API。"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from finance_agent.analysis import analyze
from finance_agent.audit import AuditLog
from finance_agent.comparatives import supplement_comparatives
from finance_agent.extraction import validate_payload
from finance_agent.parser import parse_financial_doc
from finance_agent.reporting import generate_report
from finance_agent.schemas import RunResult


def quality(report):
    required = {"revenue", "net_profit", "parent_net_profit", "adjusted_parent_net_profit", "operating_cashflow"}
    verified = [m for m in report.metrics if m.status == "verified"]
    current = [m for m in verified if m.period_end == report.period_end]
    return len({m.name for m in current} & required), len(current), len(verified)


def rebuild(run_dirs: list[Path]) -> RunResult:
    manifest = json.loads((ROOT / "samples/public/manifest.json").read_text(encoding="utf-8"))
    sources = {s["sha256"][:16]: ROOT / "samples/public" / s["file"] for s in manifest["samples"]}
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_public_" + uuid.uuid4().hex[:8]
    folder = (ROOT / "output" / run_id).resolve()
    folder.mkdir(parents=True)
    (folder / "sources").mkdir()
    audit = AuditLog(folder / "trace.jsonl")
    audit.record("rebuild_saved_live_runs", inputs={"source_runs": [str(p.resolve()) for p in run_dirs],
        "model_calls_this_rebuild": 0, "method": "重新解析原PDF，逐次重新校验已保存模型原始JSON，选取完整度最高结果后补查原表"})
    docs, candidates = {}, {}
    usage = {}
    models = set()
    partial = set()
    for source_dir in run_dirs:
        saved = RunResult.model_validate_json((source_dir / "result.json").read_text(encoding="utf-8"))
        if saved.mode != "live":
            raise ValueError("只能重核真实模型运行，不能把虚构演示变成真实结果。")
        models.add(saved.model)
        for key, count in saved.usage.items():
            if key != "replay_model_calls":
                usage[key] = usage.get(key, 0) + count
        history = folder / "history" / saved.run_id
        history.mkdir(parents=True)
        for name in ("trace.jsonl", "result.json"):
            shutil.copyfile(source_dir / name, history / name)
        active_doc = None
        sent_blocks = []
        for line in (source_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if event["tool"] == "retrieve_evidence":
                identifier = event["input"]["document_id"]
                if identifier not in sources:
                    raise ValueError(f"来源不是内置公开样本：{identifier}")
                if identifier not in docs:
                    doc = parse_financial_doc(sources[identifier])
                    if doc.id != identifier:
                        raise ValueError("原始PDF与运行摘要不一致。")
                    docs[identifier] = doc
                    shutil.copyfile(sources[identifier], folder / "sources" / f"{identifier}.pdf")
                    audit.record("parse_financial_doc", outputs=doc.model_dump(mode="json"))
                active_doc = docs[identifier]
                by_id = {b.id: b for b in active_doc.blocks}
                # 只重核实际已发送的证据，不为模型引用增加未发送材料。
                sent_blocks = [by_id[x] for x in event["output"]["evidence_ids"] if x in by_id]
                if event["output"].get("partial_document"):
                    partial.add(identifier)
            elif event["tool"] == "model_response" and active_doc is not None:
                try:
                    payload = json.loads(event["output"]["content"])
                    report = validate_payload(payload, active_doc, sent_blocks)
                except (ValueError, KeyError, TypeError):
                    audit.record("revalidate_saved_response", outputs={"document_id": active_doc.id,
                        "source_run": saved.run_id, "reason": "结构或原文核验失败"}, status="rejected")
                    continue
                audit.record("revalidate_saved_response", inputs={"source_run": saved.run_id,
                    "task": event["output"].get("task"), "model_calls": 0}, outputs=report.model_dump(mode="json"))
                previous = candidates.get(active_doc.id)
                if previous is None or quality(report) >= quality(previous):
                    candidates[active_doc.id] = report
    reports = []
    evidence = {}
    for identifier, doc in docs.items():
        if identifier not in candidates:
            raise ValueError(f"{doc.name}没有可重新核验的真实模型输出。")
        report = candidates[identifier]
        if identifier in partial:
            report.warnings.append("本次仅检索相关页面，未完整审阅所有附注；未发现信号不等于全报告无异常。")
        report = supplement_comparatives(report, doc)
        audit.record("supplement_comparatives", inputs={"document_id": identifier}, outputs=report.model_dump(mode="json"))
        reports.append(report)
        evidence.update({b.id: b for b in doc.blocks})
        print(f"{doc.name}：{len(report.metrics)}条金额记录，{len(report.disclosures)}条披露记录。")
    analysis = analyze(reports)
    usage["replay_model_calls"] = 0
    result = RunResult(run_id=run_id, mode="live", model=", ".join(sorted(models)),
        reports=reports, analysis=analysis, evidence=evidence, run_dir=str(folder), usage=usage,
        warnings=[f"这是{len(run_dirs)}次真实DeepSeek运行保存输出的重新核验与合并；本次重算未调用API。"
                  "用量统计为原运行合计，原始输入输出保存在history目录。"])
    audit.record("financial_analysis", outputs=analysis.model_dump(mode="json"))
    generate_report(result)
    audit.record("generate_report", outputs={"files": ["report.md", "metrics.csv", "result.json"], "model_calls": 0})
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=Path, nargs="+", required=True)
    args = parser.parse_args()
    result = rebuild([p.resolve() for p in args.runs])
    print(f"结果目录：{result.run_dir}")
