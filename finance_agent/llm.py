"""DeepSeek 官方 Chat Completion 接口，有限重试与调用预算。"""
from __future__ import annotations

import json
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from .audit import AuditLog
from .config import Settings


class ModelError(RuntimeError):
    pass


class DeepSeekClient:
    def __init__(self, settings: Settings, audit: AuditLog):
        if not settings.api_key:
            raise ModelError("请先在项目 .env 文件中填写 DEEPSEEK_API_KEY。")
        parsed = urlsplit(settings.base_url)
        if parsed.scheme != "https" or parsed.username or parsed.password:
            raise ModelError("模型接口地址需要使用不含账户信息的 HTTPS 地址。")
        self.settings = settings
        self.audit = audit
        self.audit.secrets.append(settings.api_key)
        if not 0 <= settings.temperature <= 2:
            raise ModelError("DEEPSEEK_TEMPERATURE需要在0至2之间。")
        self.calls = 0
        self.usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0, "model_calls": 0}
        self.returned_models: set[str] = set()

    def complete_json(self, system: str, user: str, *, task: str) -> dict[str, Any]:
        for attempt in range(2):
            if self.calls >= self.settings.max_model_calls:
                raise ModelError("本次分析已达到模型调用上限，请减少上传文件数量。")
            self.calls += 1
            self.usage["model_calls"] += 1
            payload = {
                "model": self.settings.model,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}],
                "response_format": {"type": "json_object"},
                "max_tokens": self.settings.max_output_tokens,
                "stream": False,
                "temperature": self.settings.temperature,
            }
            if urlsplit(self.settings.base_url).hostname == "api.deepseek.com":
                payload["thinking"] = {"type": "disabled"}
            self.audit.record("model_request", inputs={"task": task, "model": self.settings.model,
                              "attempt": attempt + 1, "messages": payload["messages"],
                              "parameters": {key: value for key, value in payload.items() if key != "messages"},
                              "endpoint": self.settings.base_url + "/chat/completions",
                              "timeout_seconds": self.settings.timeout})
            try:
                with httpx.Client(timeout=self.settings.timeout) as client:
                    response = client.post(self.settings.base_url + "/chat/completions",
                                           headers={"Authorization": "Bearer " + self.settings.api_key},
                                           json=payload)
                if response.status_code in (401, 403):
                    raise ModelError("DeepSeek 身份验证失败，请检查本地密钥和账户权限。")
                if response.status_code == 402:
                    raise ModelError("DeepSeek 账户余额不足。")
                if response.status_code == 429 or response.status_code >= 500:
                    self.audit.record("model_response", outputs={"http_status": response.status_code}, status="retry")
                    if attempt == 0:
                        time.sleep(1)
                        continue
                    raise ModelError("DeepSeek 暂时繁忙，请稍后重试。")
                if response.status_code != 200:
                    raise ModelError(f"模型请求失败（HTTP {response.status_code}），请检查接口和模型名称。")
                result = response.json()
                for field in ("prompt_tokens", "completion_tokens"):
                    self.usage[field] += int(result.get("usage", {}).get(field, 0))
                model = result.get("model", self.settings.model)
                self.returned_models.add(model)
                message = result["choices"][0]
                content = message["message"].get("content") or ""
                self.audit.record("model_response", outputs={"model": model, "task": task,
                                  "content": content, "usage": result.get("usage", {}),
                                  "finish_reason": message.get("finish_reason"), "id": result.get("id"),
                                  "created": result.get("created"), "system_fingerprint": result.get("system_fingerprint")})
                if message.get("finish_reason") == "length":
                    raise ModelError("模型输出被截断，请缩小任务或提高本地输出长度设置。")
                data = json.loads(content)
                if not isinstance(data, dict):
                    raise ValueError("JSON root is not object")
                return data
            except ModelError as exc:
                self.audit.exception("model_failure", exc, context={"task": task, "attempt": attempt + 1})
                raise
            except (httpx.RequestError, ValueError, KeyError, IndexError, TypeError) as exc:
                self.audit.exception("model_failure", exc, context={"task": task, "attempt": attempt + 1})
                self.audit.record("model_response", outputs={"task": task, "error": "network_or_json_error"}, status="retry")
                if attempt == 0:
                    time.sleep(1)
                    continue
                raise ModelError("模型连接超时或返回数据格式不正确，请稍后重试。") from None
        raise ModelError("模型调用失败。")
