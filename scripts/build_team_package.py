"""使用明确文件清单构建组员测试包，不读取或分发原作者的.env。"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import zipfile

ROOT = Path(__file__).resolve().parents[1]
TOP = "金融财报智能体_组员测试版"
PUBLIC_RUNS = ["20261002T155414Z_b8257b0e", "20261003T092844Z_a27d24a4",
               "20261003T121104Z_3a592051", "20261003T122508Z_public_0b0c7447"]


def public_run_paths() -> list[Path]:
    """固定历史输出供追溯，并按经过校验的指针带入当前展示结果。"""
    pointer = json.loads((ROOT / "samples/public/latest_verified_run.json").read_text(encoding="utf-8"))
    run_id = pointer.get("run_id")
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
        raise ValueError("公开样本结果指针的运行编号不合法。")
    expected_path = f"output/{run_id}/result.json"
    if pointer.get("result_path") != expected_path:
        raise ValueError("公开样本结果指针必须指向对应output运行目录中的result.json。")
    output_root = (ROOT / "output").resolve()
    latest_dir = (output_root / run_id).resolve()
    if latest_dir.parent != output_root:
        raise ValueError("公开样本运行目录超出output范围。")
    result_path = latest_dir / "result.json"
    if not result_path.is_file():
        raise ValueError("最新公开样本结果不存在，请完成核验后再打包。")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if result.get("run_id") != run_id or result.get("mode") != "live":
        raise ValueError("最新公开样本结果的运行编号或真实分析模式与指针不一致。")
    return list(dict.fromkeys([ROOT / "output" / identifier for identifier in PUBLIC_RUNS] + [latest_dir]))


def yili_run_paths() -> list[Path]:
    """携带伊利当前核验结果及其对应的两份原文证据。"""
    pointer = json.loads((ROOT / "samples/member/yili/latest_run.json").read_text(encoding="utf-8"))
    run_id = pointer.get("run_id")
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
        raise ValueError("伊利样本结果指针的运行编号不合法。")
    if pointer.get("result_path") != f"output/member_yili/{run_id}/result.json":
        raise ValueError("伊利样本结果指针必须指向对应output/member_yili运行目录中的result.json。")
    output_root = (ROOT / "output/member_yili").resolve()
    latest_dir = (output_root / run_id).resolve()
    if latest_dir.parent != output_root:
        raise ValueError("伊利样本运行目录超出output/member_yili范围。")
    result_path = latest_dir / "result.json"
    if not result_path.is_file():
        raise ValueError("最新伊利样本结果不存在，请完成核验后再打包。")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if result.get("run_id") != run_id or result.get("mode") != "live":
        raise ValueError("最新伊利样本结果的运行编号或真实分析模式与指针不一致。")
    reports = result.get("reports")
    if (not isinstance(reports, list) or len(reports) != 2
            or any(not isinstance(report, dict) for report in reports)
            or any(report.get("company") != "内蒙古伊利实业集团股份有限公司"
                   or report.get("report_type") != "annual" for report in reports)
            or {report.get("period_end") for report in reports} != {"2024-12-31", "2025-12-31"}):
        raise ValueError("伊利样本必须是同公司2024、2025两份年度报告。")
    seen_ids = set()
    for report in reports:
        document_id = report.get("document_id")
        if (not isinstance(document_id, str) or not re.fullmatch(r"[a-f0-9]{16}", document_id)
                or document_id in seen_ids):
            raise ValueError("伊利样本文档编号不合法或重复。")
        seen_ids.add(document_id)
        source = latest_dir / "sources" / f"{document_id}.pdf"
        if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest()[:16] != document_id:
            raise ValueError("伊利样本原文缺失或哈希与分析结果不一致。")
    return [latest_dir]


def public_sample_paths() -> list[Path]:
    """仅选择样本清单列出的PDF，排除开发目录中的其他历史输入。"""
    sample_root = (ROOT / "samples/public").resolve()
    manifest = json.loads((sample_root / "manifest.json").read_text(encoding="utf-8"))
    paths = []
    for sample in manifest["samples"]:
        filename = sample["file"]
        if not isinstance(filename, str) or "/" in filename or "\\" in filename:
            raise ValueError("公开样本清单中的文件名不合法。")
        path = (sample_root / filename).resolve()
        if path.parent != sample_root or path.suffix.lower() != ".pdf" or not path.is_file():
            raise ValueError("公开样本清单中的PDF不存在或超出样本目录。")
        if hashlib.sha256(path.read_bytes()).hexdigest() != sample["sha256"]:
            raise ValueError("公开样本PDF哈希与人工核对清单不一致。")
        paths.append(path)
    return paths


def package() -> Path:
    root_files = ["app.py", "README.md", "THIRD_PARTY.md", "requirements.txt", "requirements.lock.txt",
                  ".env.example", ".gitignore", "start.ps1", "setup_team.ps1", "首次安装.cmd",
                  "启动财报分析.cmd", "组员测试说明.md"]
    root_files.extend([".streamlit/config.toml", "assets/workbench.css",
                       "samples/member/yili/latest_run.json", "samples/member/yili/sources.json",
                       "output/yili_validation_20261009/manual_reference.json",
                       "output/yili_validation_20261009/manual_reference_brief.md"])
    paths = [ROOT / name for name in root_files]
    patterns = {"finance_agent": "*.py", "prompts": "*.txt", "tests": "*.py", "scripts": "*.py",
                "docs": "*.md", "samples/public": "*.json", "samples/member/yili": "*.pdf", "data/evaluation": "*.json"}
    for folder, pattern in patterns.items():
        paths.extend(p for p in (ROOT / folder).glob(pattern) if p.is_file())
    paths.extend(public_sample_paths())
    for run_dir in public_run_paths() + yili_run_paths():
        paths.extend(p for p in run_dir.rglob("*") if p.is_file())
    paths = sorted(set(paths))
    checks = []
    for path in paths:
        relative = path.relative_to(ROOT)
        if any(part in {".env", ".venv", ".idea", "__pycache__", ".pytest_cache", "tmp"} for part in relative.parts):
            raise ValueError("测试包文件清单包含不允许分发的内容。")
        content = path.read_bytes()
        if path.suffix.lower() == ".cmd":
            if b"\n" in content.replace(b"\r\n", b"") or content.startswith(b"\xef\xbb\xbf"):
                raise ValueError(f"Windows启动脚本必须使用CRLF换行且不带BOM：{relative.as_posix()}")
        if path.suffix.lower() != ".pdf" and re.search(rb"\bsk-[A-Za-z0-9_-]{20,}\b", content):
            raise ValueError(f"文件疑似含密钥，打包已停止，请在本地检查：{relative.as_posix()}")
        if relative.as_posix() == ".env.example" and re.search(rb"(?m)^DEEPSEEK_API_KEY=[ \t]*[^\r\n \t]", content):
            raise ValueError("示例配置中的密钥不是空白，打包已停止。")
        checks.append({"file": relative.as_posix(), "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)})
    dest = ROOT / "output" / "team"
    dest.mkdir(parents=True, exist_ok=True)
    archive = dest / f"{TOP}_{datetime.now():%Y%m%d_%H%M%S}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for path in paths:
            z.write(path, f"{TOP}/{path.relative_to(ROOT).as_posix()}")
        z.writestr(f"{TOP}/包内文件校验.json", json.dumps({"description": "仅含明确选定的项目源码、8份公开财报及保存结果（含伊利2024/2025年报）；未包含.env及原电脑虚拟环境", "files": checks}, ensure_ascii=False, indent=2))
    with zipfile.ZipFile(archive) as z:
        if z.testzip() is not None:
            raise ValueError("压缩包完整性检查失败。")
        assert not any(Path(name).name == ".env" for name in z.namelist())
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(".sha256").write_text(f"{checksum}  {archive.name}\n", encoding="utf-8")
    print(f"已生成：{archive}")
    print(f"大小：{archive.stat().st_size / 1024 / 1024:.2f} MB；文件：{len(paths)}份及1份校验清单")
    print("完整性检查通过；未读取或包含.env，未包含.venv、上传文件或临时目录。")
    return archive


if __name__ == "__main__":
    package()
