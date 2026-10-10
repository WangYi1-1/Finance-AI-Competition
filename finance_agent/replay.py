"""离线重算已核验数据与原表，不读取密钥、不调用模型、不覆盖原运行。

这是结构化数据回放；不等同于重新让模型提取，或已重新审计每个历史模型响应。
"""
from __future__ import annotations

import shutil
import re
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .analysis import analyze
from .audit import AuditLog
from .config import ROOT
from .enrichment import enrich_report
from .parser import parse_financial_doc
from .provenance import runtime_provenance, source_record, seal_run, sha256, verify_artifacts
from .reporting import generate_report
from .schemas import RunResult


def preserve_model_history(source: Path, destination: Path, search_roots=None):
    """兼容旧版平铺/分运行历史；只把实际含模型响应的记录标为模型历史。

    复制原文件字节，不改写历史事件。便携副本自带历史后，无须原电脑路径。
    """
    roots = search_roots if search_roots is not None else (ROOT / "output", ROOT / "output/member_yili")
    history = destination / "history"
    history.mkdir()
    for name in ("result.json", "trace.jsonl"):
        if (source.parent / name).is_file():
            shutil.copyfile(source.parent / name, history / f"previous_{name}")
    found, visited = {}, set()
    current = source.parent.resolve()
    while current not in visited:
        visited.add(current)
        if (current / "artifacts.json").is_file():
            errors = verify_artifacts(current)
            if errors:
                raise ValueError("历史运行完整性检查失败：" + "；".join(errors))
        pairs = [(current / "result.json", current / "trace.jsonl")]
        prior = current / "history"
        if prior.is_dir():
            for result_file in prior.rglob("*.json"):
                if result_file.name in ("result.json", "original_result.json"):
                    trace_name = "original_trace.jsonl" if result_file.name.startswith("original_") else "trace.jsonl"
                    pairs.append((result_file, result_file.with_name(trace_name)))
        for result_file, trace_file in pairs:
            if not (result_file.is_file() and trace_file.is_file()):
                continue
            if not all(p.resolve().is_relative_to(current) for p in (result_file, trace_file)):
                raise ValueError("历史文件路径越界。")
            events = [json.loads(line) for line in trace_file.read_text(encoding="utf-8").splitlines() if line.strip()]
            responses = sum(e.get("tool") == "model_response" and bool((e.get("output") or {}).get("content")) for e in events)
            if not responses:
                continue
            saved = RunResult.model_validate_json(result_file.read_text(encoding="utf-8"))
            if not re.fullmatch(r"[A-Za-z0-9_-]+", saved.run_id):
                raise ValueError("历史模型运行编号不合法。")
            hashes = (sha256(result_file), sha256(trace_file))
            if saved.run_id in found:
                if found[saved.run_id]["hashes"] != hashes:
                    raise ValueError("同一历史模型运行编号存在冲突副本。")
                continue
            target = history / saved.run_id
            target.mkdir()
            shutil.copyfile(result_file, target / "result.json")
            shutil.copyfile(trace_file, target / "trace.jsonl")
            found[saved.run_id] = {"run_id": saved.run_id, "hashes": hashes,
                "result_path": f"history/{saved.run_id}/result.json", "trace_path": f"history/{saved.run_id}/trace.jsonl",
                "result_sha256": hashes[0], "trace_sha256": hashes[1], "model_responses": responses,
                "model_requests": sum(e.get("tool") == "model_request" for e in events)}
        replay = RunResult.model_validate_json((current / "result.json").read_text(encoding="utf-8")).provenance.get("replay", {})
        previous = replay.get("original_run_id")
        if not isinstance(previous, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", previous):
            break
        candidates = list({(Path(root) / previous).resolve() for root in roots if (Path(root) / previous / "result.json").is_file()})
        if len(candidates) != 1:
            break
        current = candidates[0]
    return [{k: v for k, v in found[run_id].items() if k != "hashes"} for run_id in sorted(found)]


def replay_result(source: Path, output_root: Path = ROOT / "output") -> RunResult:
    source = source.resolve()
    original = RunResult.model_validate_json(source.read_text(encoding="utf-8"))
    if (source.parent / "artifacts.json").is_file():
        errors = verify_artifacts(source.parent)
        if errors:
            raise ValueError("原运行完整性检查失败：" + "；".join(errors))
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_replay_" + uuid.uuid4().hex[:8]
    folder = (output_root / run_id).resolve()
    folder.mkdir(parents=True)
    (folder / "sources").mkdir()
    model_history = preserve_model_history(source, folder)
    audit = AuditLog(folder / "trace.jsonl")
    provenance = runtime_provenance("validated_data_replay")
    provenance["replay"] = {"original_run_id": original.run_id, "original_result_sha256": sha256(source),
                            "original_code_sha256": original.provenance.get("code", {}).get("sha256"),
                            "model_calls": 0, "model_history": model_history,
                            "scope": "已核验结构化金额离线重算；重解析原PDF补原因与调节明细；不是模型重新提取。"}
    audit.record("replay_start", outputs=provenance)
    reports, evidence = [], {}
    for old in original.reports:
        if not re.fullmatch(r"[0-9a-f]{16}", old.document_id):
            raise ValueError("原运行文档编号不合法。")
        pdf = source.parent / "sources" / f"{old.document_id}.pdf"
        if not pdf.is_file() or sha256(pdf)[:16] != old.document_id:
            raise ValueError(f"来源PDF缺失或哈希不符：{old.document_name}")
        audit.record("file_read_requested", inputs={"file": old.document_name, "source_run": original.run_id})
        doc = parse_financial_doc(pdf)
        doc.name = old.document_name
        for block in doc.blocks:
            block.document_name = old.document_name
        # 旧金额引用必须仍能定位，且解析器版本改变不能悄悄重绑原证据。
        by_id = {b.id: b for b in doc.blocks}
        referenced = {eid for m in old.metrics for eid in m.evidence_ids}
        for eid in referenced:
            if eid not in by_id or eid not in original.evidence or by_id[eid].text != original.evidence[eid].text:
                raise ValueError(f"原指标证据定位改变，需重新核验后再回放：{eid}")
        destination = folder / "sources" / pdf.name
        shutil.copyfile(pdf, destination)
        provenance["sources"].append(source_record(doc, destination))
        audit.record("parse_financial_doc", outputs=doc.model_dump(mode="json"))
        report = enrich_report(old, doc)
        evidence.update(by_id)
        reports.append(report)
        audit.record("enrich_financial_details", inputs={"document_id": doc.id},
                     outputs={"reasons": len(report.performance_reasons), "items": len(report.financial_details), "checks": report.detail_checks})
    analysis = analyze(reports)
    audit.record("financial_analysis", outputs=analysis.model_dump(mode="json"))
    result = original.model_copy(update={"run_id": run_id, "run_dir": str(folder), "reports": reports,
        "analysis": analysis, "evidence": evidence, "usage": {**original.usage, "replay_model_calls": 0},
        "provenance": provenance})
    generate_report(result)
    audit.record("generate_report", outputs={"files": ["report.md", "metrics.csv", "analysis.xlsx", "result.json"]})
    seal_run(result, audit)
    return result
