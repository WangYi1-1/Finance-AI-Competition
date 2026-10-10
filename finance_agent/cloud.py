"""可选云端测试的访问、会话目录与并发控制；不保存访问口令或API密钥。"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import hmac
from io import BytesIO
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
import time
from zipfile import ZipFile, ZIP_DEFLATED


def is_cloud() -> bool:
    return os.environ.get("FINANCE_CLOUD") == "1"


class CloudError(RuntimeError):
    pass


@dataclass(frozen=True)
class CloudConfig:
    access_code: str = field(repr=False)
    runtime_root: Path
    retention_hours: int = 24

    @classmethod
    def load(cls):
        code = os.environ.get("TEAM_ACCESS_CODE", "")
        if len(code) < 12 or not code.strip():
            raise CloudError("云端尚未配置组员访问口令，请管理员在Secrets中设置至少12位的TEAM_ACCESS_CODE。")
        root = Path(os.environ.get("FINANCE_RUNTIME_DIR") or Path(tempfile.gettempdir()) / "bjfu_finance_cloud").resolve()
        return cls(code, root)

    @property
    def credential_version(self):
        # 用服务器密钥作HMAC，避免将可离线猜测口令的裸SHA放入会话状态。
        return hmac.new(self.access_code.encode(), b"cloud-access-version", hashlib.sha256).hexdigest()

    def accepts(self, supplied: str) -> bool:
        return hmac.compare_digest(supplied.encode("utf-8"), self.access_code.encode("utf-8"))


def session_paths(config: CloudConfig, session_id: str) -> tuple[Path, Path]:
    if not re.fullmatch(r"[a-f0-9]{32}", session_id):
        raise CloudError("会话已失效，请重新进入工作台。")
    root = (config.runtime_root / "sessions").resolve()
    folder = (root / session_id).resolve()
    if folder.parent != root:
        raise CloudError("会话目录无效。")
    uploads, outputs = folder / "uploads", folder / "output"
    uploads.mkdir(parents=True, exist_ok=True)
    outputs.mkdir(exist_ok=True)
    (folder / "last_active").touch()
    return uploads, outputs


def clean_expired_sessions(config: CloudConfig, current_session: str):
    """只清理本应用生成的过期云端会话；不触及本地项目output/uploads。"""
    root = (config.runtime_root / "sessions").resolve()
    if not root.is_dir():
        return
    now = time.time()
    for folder in root.iterdir():
        if folder.name == current_session or not re.fullmatch(r"[a-f0-9]{32}", folder.name) or folder.is_symlink():
            continue
        target = folder.resolve()
        if target.parent != root or not target.is_dir():
            continue
        activity = target / "last_active"
        try:
            if activity.is_file() and now - activity.stat().st_mtime > config.retention_hours * 3600:
                shutil.rmtree(target)
        except OSError:
            # 多个页面同时清理或下载中的文件暂时不可删除时，下次活动再试。
            continue


_locks_guard = threading.Lock()
_analysis_locks: dict[str, threading.Lock] = {}


def _lock_for(root: Path):
    with _locks_guard:
        return _analysis_locks.setdefault(str(root.resolve()), threading.Lock())


@contextmanager
def analysis_slot(config: CloudConfig, mode: str = "live"):
    """每服务进程同时一个分析；不限制每天或累计分析次数。"""
    lock = _lock_for(config.runtime_root)
    if not lock.acquire(blocking=False):
        raise CloudError("其他组员正在分析，请等这次任务结束后再试。查看已有成果不受影响。")
    try:
        yield
    finally:
        lock.release()


def build_run_archive(run_dir: Path) -> bytes:
    """完整保留封存清单、来源与历史，仅打包清单内文件。"""
    import json
    from .provenance import verify_artifacts
    run_dir = run_dir.resolve()
    errors = verify_artifacts(run_dir)
    if errors:
        raise CloudError("成果完整性检查失败，请联系管理员；没有生成可复现下载包。")
    manifest = json.loads((run_dir / "artifacts.json").read_text(encoding="utf-8"))
    run_id = manifest["run_id"]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
        raise CloudError("运行编号不合法。")
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        for item in manifest["files"] + [{"file": "artifacts.json"}]:
            path = (run_dir / item["file"]).resolve()
            if not path.is_relative_to(run_dir):
                raise CloudError("成果文件路径无效。")
            archive.write(path, f"{run_id}/{path.relative_to(run_dir).as_posix()}")
    return buffer.getvalue()
