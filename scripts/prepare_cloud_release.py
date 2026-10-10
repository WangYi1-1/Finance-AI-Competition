"""生成供 GitHub / Streamlit 使用的干净发布目录；不登录、不上传、不读取密钥。"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import tomllib
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
MAX_GITHUB_FILE_BYTES = 100 * 1024 * 1024
ROOT_FILES = (
    "app.py", "cloud_app.py", "README.md", "THIRD_PARTY.md",
    "requirements.txt", "requirements.lock.txt", ".env.example", "start.ps1", "setup_team.ps1",
    "首次安装.cmd", "启动财报分析.cmd", "组员测试说明.md", "发布GitHub更新.cmd",
    "update_team.ps1", "更新到最新版.cmd", ".streamlit/config.toml",
    ".streamlit/secrets.example.toml", "scripts/prepare_cloud_release.py",
    "scripts/publish_cloud_update.py", "docs/云端部署与更新.md",
    "output/yili_validation_20261009/manual_reference.json",
    "output/yili_validation_20261009/manual_reference_brief.md",
)
PATTERNS = (("finance_agent", "*.py"), ("prompts", "*.txt"),
            ("assets", "*.css"), ("docs", "*.md"), ("data/evaluation", "*.json"),
            ("scripts", "*.py"), ("tests", "*.py"))
DATASETS = (
    ("public", "samples/public/manifest.json", "samples/public/latest_verified_run.json", "output"),
    ("yili", "samples/member/yili/sources.json", "samples/member/yili/latest_run.json", "output/member_yili"),
)
RUN_REQUIRED = {"result.json", "report.md", "metrics.csv", "analysis.xlsx", "trace.jsonl"}
SECRET_PATTERNS = (
    re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(rb"\bBearer\s+[A-Za-z0-9_.-]{24,}", re.IGNORECASE),
)


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def safe_relative(name: str) -> PurePosixPath:
    if not isinstance(name, str) or not name or "\\" in name or ":" in name:
        raise ValueError("发布清单含不合法的文件路径。")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {".", ".."} for part in name.split("/")) or str(path) != name:
        raise ValueError("发布清单含不合法的文件路径。")
    if any(part in {".env", ".venv", "__pycache__", ".pytest_cache", "tmp", "uploads", "secrets.toml"}
           for part in path.parts):
        raise ValueError("发布清单包含不允许发布的私有或临时文件。")
    return path


def read_file(root: Path, name: str) -> bytes:
    relative = safe_relative(name)
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError(f"发布所需文件不存在或越界：{name}")
    # 已入选文件也不能借符号链接指向根目录内的配置秘密。
    safe_relative(path.relative_to(root).as_posix())
    if path.stat().st_size > MAX_GITHUB_FILE_BYTES:
        raise ValueError(f"文件超过 GitHub 普通仓库的单文件大小限制：{name}")
    return path.read_bytes()


def read_json(root: Path, name: str):
    return json.loads(read_file(root, name).decode("utf-8"))


def sample_files(root: Path, manifest_name: str) -> tuple[list[str], dict[str, str]]:
    manifest = read_json(root, manifest_name)
    samples = manifest if isinstance(manifest, list) else manifest.get("samples", [])
    if not isinstance(samples, list) or not samples:
        raise ValueError("公开样本清单为空或结构错误。")
    paths, hashes = [manifest_name], {}
    sample_dir = PurePosixPath(manifest_name).parent
    for sample in samples:
        if not isinstance(sample, dict):
            raise ValueError("公开样本清单结构错误。")
        name = sample.get("file")
        relative = safe_relative(name)
        if len(relative.parts) != 1 or relative.suffix.lower() != ".pdf":
            raise ValueError("公开样本文件名必须是样本目录内的 PDF 文件名。")
        name = str(sample_dir / relative)
        expected = sample.get("sha256")
        content = read_file(root, name)
        if not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected) or digest(content) != expected:
            raise ValueError(f"公开样本 PDF 哈希不匹配：{name}")
        if not content.startswith(b"%PDF-"):
            raise ValueError(f"公开样本不是 PDF：{name}")
        document_id = expected[:16]
        if document_id in hashes:
            raise ValueError("公开样本清单有重复文档。")
        hashes[document_id] = expected
        paths.append(name)
    return paths, hashes


def allowed_artifact(name: str) -> bool:
    relative = safe_relative(name)
    if name in RUN_REQUIRED:
        return True
    if len(relative.parts) == 2 and relative.parts[0] == "sources":
        return bool(re.fullmatch(r"[a-f0-9]{16}\.pdf", relative.name))
    if relative.parts[0] == "history":
        history_names = {"result.json", "trace.jsonl", "previous_result.json", "previous_trace.jsonl",
                         "original_result.json", "original_trace.jsonl"}
        return relative.name in history_names and all(
            re.fullmatch(r"[A-Za-z0-9_-]+", part) for part in relative.parts[1:-1])
    return False


def selected_run(root: Path, pointer_name: str, output_prefix: str, expected_sources: dict[str, str]):
    pointer = read_json(root, pointer_name)
    run_id = pointer.get("run_id")
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
        raise ValueError("最新结果指针的运行编号不合法。")
    prefix = f"{output_prefix}/{run_id}"
    result_name = f"{prefix}/result.json"
    if pointer.get("result_path") != result_name:
        raise ValueError("最新结果指针必须指向对应运行目录。")
    result = read_json(root, result_name)
    manifest_name = f"{prefix}/artifacts.json"
    manifest = read_json(root, manifest_name)
    if result.get("run_id") != run_id or result.get("mode") != "live" or manifest.get("run_id") != run_id:
        raise ValueError("保存结果与清单的运行编号或真实分析模式不符。")
    if manifest.get("schema_version") != 1:
        raise ValueError("保存结果文件清单版本不支持。")
    files = manifest.get("files")
    if not isinstance(files, list):
        raise ValueError("保存结果文件清单结构错误。")
    paths, seen = [pointer_name, manifest_name], set()
    for record in files:
        if not isinstance(record, dict):
            raise ValueError("保存结果文件清单结构错误。")
        name = record.get("file")
        if not allowed_artifact(name) or name in seen:
            raise ValueError("保存结果清单包含重复或不允许发布的文件。")
        seen.add(name)
        path = f"{prefix}/{name}"
        content = read_file(root, path)
        if len(content) != record.get("bytes") or digest(content) != record.get("sha256"):
            raise ValueError(f"封存文件校验失败：{path}")
        paths.append(path)
    if not RUN_REQUIRED.issubset(seen):
        raise ValueError("保存结果清单缺少核心成果文件。")
    reports = result.get("reports")
    if not isinstance(reports, list) or any(not isinstance(report, dict) for report in reports):
        raise ValueError("保存结果财报结构错误。")
    ids = [report.get("document_id") for report in reports]
    if len(ids) != len(set(ids)) or set(ids) != set(expected_sources):
        raise ValueError("保存结果的财报与公开样本清单不一致。")
    source_records = result.get("provenance", {}).get("sources", [])
    if not isinstance(source_records, list) or any(not isinstance(source, dict) for source in source_records):
        raise ValueError("保存结果缺少可核验的完整来源哈希。")
    source_hashes = {source.get("document_id"): source.get("sha256") for source in source_records}
    if len(source_records) != len(source_hashes) or source_hashes != expected_sources:
        raise ValueError("保存结果的完整来源哈希与公开样本不一致。")
    for document_id, expected in expected_sources.items():
        name = f"sources/{document_id}.pdf"
        if name not in seen or digest(read_file(root, f"{prefix}/{name}")) != expected:
            raise ValueError("保存结果原文 PDF 未封存或完整来源哈希不符。")
    return paths, {"run_id": run_id, "result_path": result_name, "report_count": len(reports)}


def check_content(name: str, content: bytes):
    """只扫描明确入选的内容；异常仅显示文件名，不回显匹配到的秘密。"""
    if PurePosixPath(name).suffix.lower() == ".pdf":
        return
    payloads = [content]
    if PurePosixPath(name).suffix.lower() == ".xlsx":
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            payloads = [archive.read(member) for member in archive.namelist() if member.endswith(".xml")]
    if any(pattern.search(payload) for payload in payloads for pattern in SECRET_PATTERNS):
        raise ValueError(f"文件疑似包含密钥，停止生成发布目录；请在本地检查：{name}")
    if name == ".streamlit/secrets.example.toml":
        example = tomllib.loads(content.decode("utf-8"))
        if example.get("DEEPSEEK_API_KEY") or example.get("TEAM_ACCESS_CODE"):
            raise ValueError("云端配置示例的密钥和访问码必须为空。")
    if name == ".env.example":
        assignment = re.search(r"(?m)^DEEPSEEK_API_KEY[ \t]*=[ \t]*([^\r\n]*)", content.decode("utf-8"))
        if assignment is None or assignment.group(1).strip() not in {"", "\"\"", "''"}:
            raise ValueError("本地配置示例的 DEEPSEEK_API_KEY 必须为空。")


def release_gitignore(showcase: dict) -> str:
    public_id, yili_id = showcase["public"]["run_id"], showcase["yili"]["run_id"]
    return "\n".join((
        "# 仅跟踪这次审核过的展示成果；用户运行目录不在仓库内。",
        ".venv/", ".env", ".env.*", "!.env.example", "__pycache__/", ".pytest_cache/", "*.pyc",
        ".streamlit/secrets.toml", "tmp/", "uploads/", "data/uploads/", "deploy/",
        "/output/*", f"!/output/{public_id}/", "!/output/member_yili/",
        "/output/member_yili/*", f"!/output/member_yili/{yili_id}/",
        "!/output/yili_validation_20261009/", "/output/yili_validation_20261009/*",
        "!/output/yili_validation_20261009/manual_reference.json",
        "!/output/yili_validation_20261009/manual_reference_brief.md", "",
    ))


def selected_files(root: Path):
    names = list(ROOT_FILES)
    for folder, pattern in PATTERNS:
        names.extend(path.relative_to(root).as_posix() for path in (root / folder).glob(pattern) if path.is_file())
    showcase = {}
    for label, manifest, pointer, output_prefix in DATASETS:
        sample_paths, sources = sample_files(root, manifest)
        run_paths, summary = selected_run(root, pointer, output_prefix, sources)
        names.extend(sample_paths)
        names.extend(run_paths)
        showcase[label] = summary
    return sorted(set(names)), showcase


def prepare(root: Path = ROOT) -> Path:
    root = root.resolve()
    names, showcase = selected_files(root)
    payloads = {}
    for name in names:
        content = read_file(root, name)
        check_content(name, content)
        payloads[name] = content
    payloads[".gitignore"] = release_gitignore(showcase).encode("utf-8")
    # 原始日志与封存成果以字节哈希核验，Git 不得改变 CRLF/LF。
    payloads[".gitattributes"] = b"# Preserve sealed artifact bytes across Windows and Linux.\n* -text\n"
    checks = [{"file": name, "bytes": len(content), "sha256": digest(content)}
              for name, content in sorted(payloads.items())]
    timestamp = datetime.now(timezone.utc)
    snapshot = {"schema_version": 1, "created_at_utc": timestamp.isoformat(), "status": "ready_for_review",
                "entrypoint": "app.py", "python_version": "3.13", "showcase": showcase,
                "files": checks, "total_bytes": sum(item["bytes"] for item in checks),
                "note": "只含明确选择的源码、公开样本及最新两组封存成果；未读取密钥，未登录或上传。"}
    payloads["cloud_snapshot.json"] = (json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    parent = (root / "deploy/releases").resolve()
    if not parent.is_relative_to(root):
        raise ValueError("发布目录越界。")
    folder = parent / f"{timestamp:%Y%m%dT%H%M%SZ}_{uuid.uuid4().hex[:8]}"
    folder.mkdir(parents=True, exist_ok=False)
    for name, content in payloads.items():
        target = folder / safe_relative(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    errors = verify_snapshot(folder)
    if errors:
        raise ValueError("发布副本校验失败：" + "; ".join(errors))
    return folder


def verify_snapshot(folder: Path) -> list[str]:
    folder = folder.resolve()
    snapshot = read_json(folder, "cloud_snapshot.json")
    errors, expected_names = [], {"cloud_snapshot.json"}
    for record in snapshot["files"]:
        name = record["file"]
        if name in expected_names:
            errors.append("发布清单文件路径重复")
            continue
        expected_names.add(name)
        try:
            content = read_file(folder, name)
            check_content(name, content)
            if len(content) != record["bytes"] or digest(content) != record["sha256"]:
                errors.append(f"发布副本内容不符：{name}")
        except (ValueError, FileNotFoundError):
            errors.append(f"发布副本文件缺失或不合法：{name}")
    actual_names = {path.relative_to(folder).as_posix() for path in folder.rglob("*")
                    if path.relative_to(folder).parts[0] != ".git" and path.is_file()}
    if actual_names != expected_names:
        errors.append("发布副本中存在未列出的文件或遗漏文件")
    try:
        selected_names, _ = selected_files(folder)
        if set(selected_names) | {".gitignore", ".gitattributes", "cloud_snapshot.json"} != expected_names:
            errors.append("发布副本文件不属于明确发布白名单")
    except (ValueError, KeyError, TypeError, FileNotFoundError):
        errors.append("发布副本缺少白名单要求的源码或工具")
    for label, manifest, pointer, output_prefix in DATASETS:
        try:
            _, sources = sample_files(folder, manifest)
            _, summary = selected_run(folder, pointer, output_prefix, sources)
            if summary != snapshot["showcase"][label]:
                errors.append("发布副本最新成果指针与快照不一致")
        except (ValueError, FileNotFoundError, KeyError, TypeError):
            errors.append(f"发布副本展示成果不完整：{label}")
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", type=Path, help="只读核验已生成的发布目录；不上传")
    args = parser.parse_args()
    if args.verify:
        errors = verify_snapshot(args.verify)
        if errors:
            raise SystemExit("发布目录核验未通过：" + "; ".join(errors))
        print("发布目录核验通过；未读取密钥，未上传文件。")
    else:
        folder = prepare()
        snapshot = read_json(folder, "cloud_snapshot.json")
        print(f"已生成云端发布目录：{folder}")
        print(f"文件：{len(snapshot['files']) + 1}；大小：{snapshot['total_bytes'] / 1024 / 1024:.2f} MiB")
        print("最新两组展示成果及完整来源校验通过；未读取密钥，未登录或上传。")


if __name__ == "__main__":
    main()
