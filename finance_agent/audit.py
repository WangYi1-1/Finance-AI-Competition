"""保存工具的可审计执行轨迹，不记录 API 密钥或模型隐藏思维。"""
from __future__ import annotations

import json
import re
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class AuditLog:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.secrets = []

    def redact(self, text: str) -> str:
        for secret in self.secrets:
            if secret:
                text = text.replace(secret, "[已脱敏]")
        return re.sub(r"(?i)(Bearer\s+)[^\s\"']+|sk-[A-Za-z0-9_-]+", "[已脱敏]", text)

    def exception(self, tool: str, exc: Exception, *, context=None):
        self.record(tool, inputs=context, outputs={"exception_type": type(exc).__name__,
                    "message": self.redact(str(exc)), "stack": self.redact(
                    "".join(traceback.TracebackException.from_exception(exc, capture_locals=False).format()))}, status="failed")

    def record(self, tool: str, *, inputs: Any = None, outputs: Any = None,
               status: str = "ok") -> None:
        event = {"time_utc": datetime.now(timezone.utc).isoformat(),
                 "tool": tool, "status": status, "input": inputs, "output": outputs}
        with self.path.open("a", encoding="utf-8") as stream:
            # 在结构化前脱敏每个文本字段，避免破坏JSON语法。
            def sanitize(value):
                if isinstance(value, str):
                    return self.redact(value)
                if isinstance(value, dict):
                    return {key: sanitize(item) for key, item in value.items()}
                if isinstance(value, (list, tuple)):
                    return [sanitize(item) for item in value]
                return value
            stream.write(json.dumps(sanitize(event), ensure_ascii=False, default=str) + "\n")
