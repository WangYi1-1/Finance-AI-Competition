"""绑定来源、程序版本、环境与最终产物；清单不自引用哈希。"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
import os
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .config import ROOT


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def sha256(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def code_fingerprint(root: Path = ROOT):
    files = [root / name for name in ("app.py", "cloud_app.py", "requirements.txt", "requirements.lock.txt")]
    for directory, pattern in (("finance_agent", "*.py"), ("prompts", "*.txt"), ("assets", "*.css")):
        files.extend((root / directory).glob(pattern))
    files = sorted(set(p for p in files if p.is_file()), key=lambda p: p.relative_to(root).as_posix())
    hashes = {p.relative_to(root).as_posix(): sha256(p) for p in files}
    canonical = json.dumps(hashes, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return {"sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(), "files": hashes}


def runtime_provenance(kind="live"):
    from .analysis import DEFAULT_RULES
    packages = {}
    for name in ("PyMuPDF", "pydantic", "httpx", "streamlit", "pandas", "altair", "python-dotenv"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = "未安装"
    return {"generated_at_utc": utc_now(), "execution_kind": kind,
            "code": code_fingerprint(), "environment": {"python": platform.python_version(),
            "platform": platform.platform(), "packages": packages}, "sources": [],
            "rule_parameters": {k: str(v) for k, v in asdict(DEFAULT_RULES).items()},
            "hosting": "cloud_team_test" if os.environ.get('FINANCE_CLOUD') == '1' else "local",
            "boundary": ("云端小组测试，指定PDF和服务器配置；新任务文件暂存在会话目录，应下载备份。" if os.environ.get('FINANCE_CLOUD') == '1'
                         else "本流程仅解析指定PDF及本地配置；") + "模型使用检索片段。非操作系统级沙箱；实时模式联网调用配置的模型接口。"}


def source_record(doc, source_pdf: Path):
    known = []
    for path in (ROOT / "samples/public/manifest.json", ROOT / "samples/member/yili/sources.json"):
        if path.is_file():
            value = json.loads(path.read_text(encoding="utf-8"))
            known.extend(value if isinstance(value, list) else value.get("samples", value.get("sources", [])))
    match = next((x for x in known if x.get("sha256") == doc.sha256), {})
    return {"document_id": doc.id, "name": doc.name, "sha256": doc.sha256,
            "bytes": source_pdf.stat().st_size, "pages": doc.page_count,
            "local_path": f"sources/{doc.id}.pdf", "official_url": match.get("url"),
            "downloaded_at_utc": match.get("downloaded_at_utc"), "checked_at_utc": utc_now(),
            "time_note": "checked_at为本次核对时间；未记录的原始下载时间不补造。"}


def seal_run(result, audit):
    folder = Path(result.run_dir)
    audit.record("run_complete", outputs={"run_id": result.run_id,
                 "manifest": "artifacts.json", "artifact_hashes": "最终清单在日志关闭后生成，清单不包含自身哈希。"})
    files = [folder / name for name in ("result.json", "report.md", "metrics.csv", "analysis.xlsx", "trace.jsonl")]
    files.extend((folder / "sources").glob("*.pdf"))
    files.extend(p for p in (folder / 'history').rglob('*') if p.is_file())
    records = [{"file": p.relative_to(folder).as_posix(), "bytes": p.stat().st_size,
                "sha256": sha256(p)} for p in files if p.is_file()]
    manifest = {"schema_version": 1, "run_id": result.run_id, "sealed_at_utc": utc_now(),
                "code_sha256": result.provenance.get("code", {}).get("sha256"), "files": records,
                "note": "清单记录最终产物，排除自身以避免循环；不再向已封存日志追加事件。"}
    (folder / "artifacts.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def verify_artifacts(folder: Path):
    folder = folder.resolve()
    manifest = json.loads((folder / "artifacts.json").read_text(encoding="utf-8"))
    errors = []
    names = set()
    for record in manifest["files"]:
        path = (folder / record["file"]).resolve()
        if not path.is_relative_to(folder) or record["file"] in names:
            errors.append("清单路径越界或重复")
            continue
        names.add(record["file"])
        if not path.is_file() or path.stat().st_size != record["bytes"] or sha256(path) != record["sha256"]:
            errors.append(f"文件缺失或内容不符：{record['file']}")
    for name in ("result.json", "report.md", "metrics.csv", "trace.jsonl"):
        if name not in names:
            errors.append(f"清单缺少核心产物：{name}")
    if "result.json" in names and (folder / "result.json").is_file():
        run_id = json.loads((folder / "result.json").read_text(encoding="utf-8"))["run_id"]
        if manifest.get("run_id") != run_id:
            errors.append("清单与结果运行编号不符")
    return errors
