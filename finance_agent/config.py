"""只从本地配置读取密钥，不把配置对象写入日志。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    api_key: str = field(repr=False, default="")
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-flash"
    timeout: float = 120
    max_model_calls: int = 12
    max_output_tokens: int = 6500
    temperature: float = 0.0

    @classmethod
    def load(cls, root: Path | None = None) -> "Settings":
        cloud = os.environ.get("FINANCE_CLOUD") == "1"
        local = {} if cloud else dotenv_values((root or ROOT) / ".env")

        def get(key: str, default: str = "") -> str:
            return os.environ.get(key, local.get(key) or default).strip()

        result = cls(
            api_key=get("DEEPSEEK_API_KEY"),
            base_url=get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/"),
            model=get("DEEPSEEK_MODEL", "deepseek-flash"),
            timeout=float(get("DEEPSEEK_TIMEOUT", "120")),
            max_model_calls=int(get("MAX_MODEL_CALLS", "12")),
            max_output_tokens=int(get("MAX_OUTPUT_TOKENS", "6500")),
            temperature=float(get("DEEPSEEK_TEMPERATURE", "0")),
        )
        if cloud and (not 1 <= result.max_model_calls <= 12 or not 1 <= result.max_output_tokens <= 6500
                      or not 1 <= result.timeout <= 120):
            raise ValueError("云端每次模型调用为1至12次，输出长度1至6500，超时1至120秒。")
        return result
