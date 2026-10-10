"""DeepSeek 调用协议与失败边界；MockTransport 拦截全部请求。

这些测试只使用虚构密钥和直接构造的 Settings，绝不读取本地 .env。
"""
import json

import httpx
import pytest

from finance_agent.audit import AuditLog
from finance_agent.config import Settings
from finance_agent.llm import DeepSeekClient, ModelError
import finance_agent.llm as llm_module


TEST_KEY = "test-secret-only"


def completion(content='{"answer": 42}', *, finish_reason="stop", model="deepseek-returned-test"):
    return {
        "model": model,
        "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 7},
    }


@pytest.fixture
def mocked_client(monkeypatch, tmp_path):
    real_client = httpx.Client
    requests = []
    responses = []
    sleeps = []

    def forbid_environment_loading(*args, **kwargs):
        raise AssertionError("测试禁止加载 .env")

    def handler(request):
        requests.append(request)
        if not responses:
            raise AssertionError("模拟响应已耗尽；不允许发送其他请求")
        status, body = responses.pop(0)
        return httpx.Response(status, json=body, request=request)

    transport = httpx.MockTransport(handler)

    def fake_client(**kwargs):
        return real_client(transport=transport, **kwargs)

    monkeypatch.setattr(llm_module.httpx, "Client", fake_client)
    monkeypatch.setattr(llm_module.time, "sleep", sleeps.append)
    monkeypatch.setattr("finance_agent.config.dotenv_values", forbid_environment_loading)
    monkeypatch.setattr(Settings, "load", forbid_environment_loading)
    log_path = tmp_path / "trace.jsonl"

    def build(*, max_calls=3):
        settings = Settings(api_key=TEST_KEY, timeout=1, max_model_calls=max_calls,
                            max_output_tokens=100, model="deepseek-request-test")
        return DeepSeekClient(settings, AuditLog(log_path))

    return build, requests, responses, sleeps, log_path


def test_json_success_records_usage_and_returned_model(mocked_client):
    build, requests, responses, sleeps, log_path = mocked_client
    responses.append((200, completion()))
    client = build()
    assert client.complete_json("返回 JSON", "测试内容", task="extract") == {"answer": 42}
    assert client.usage == {"prompt_tokens": 12, "completion_tokens": 7, "model_calls": 1}
    assert client.returned_models == {"deepseek-returned-test"}
    assert len(requests) == 1
    payload = json.loads(requests[0].content)
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["thinking"] == {"type": "disabled"}
    assert payload["temperature"] == 0
    assert sleeps == []
    events = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert events[-1]["output"]["model"] == "deepseek-returned-test"
    assert events[-1]["output"]["usage"]["completion_tokens"] == 7
    assert events[0]["input"]["parameters"]["max_tokens"] == payload["max_tokens"]
    assert events[0]["input"]["parameters"]["temperature"] == 0
    assert TEST_KEY not in log_path.read_text(encoding="utf-8")


def test_401_returns_safe_error_without_secret_in_trace(mocked_client):
    build, requests, responses, sleeps, log_path = mocked_client
    # 即使服务端错误正文回显密钥，应用也只给出固定的安全错误说明。
    responses.append((401, {"error": {"message": f"invalid key: {TEST_KEY}"}}))
    client = build()
    with pytest.raises(ModelError, match="身份验证失败") as error:
        client.complete_json("返回 JSON", "测试内容", task="extract")
    assert len(requests) == 1
    assert requests[0].headers["Authorization"] == "Bearer " + TEST_KEY
    assert sleeps == []
    assert TEST_KEY not in str(error.value)
    assert TEST_KEY not in repr(client.settings)
    assert TEST_KEY not in log_path.read_text(encoding="utf-8")


def test_429_retries_once_and_every_attempt_counts_against_budget(mocked_client):
    build, requests, responses, sleeps, log_path = mocked_client
    responses.extend([(429, {"error": {"message": "rate limited"}}), (200, completion())])
    client = build(max_calls=2)
    assert client.complete_json("返回 JSON", "测试内容", task="extract") == {"answer": 42}
    assert len(requests) == 2
    assert client.calls == client.usage["model_calls"] == 2
    assert client.usage["prompt_tokens"] == 12
    assert sleeps == [1]
    with pytest.raises(ModelError, match="调用上限"):
        client.complete_json("返回 JSON", "另一个任务", task="again")
    assert len(requests) == 2
    assert client.calls == 2
    events = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert any(event["status"] == "retry" and event["output"] == {"http_status": 429} for event in events)


def test_truncated_json_has_explicit_error_without_retry(mocked_client):
    build, requests, responses, sleeps, _ = mocked_client
    responses.append((200, completion('{"answer":', finish_reason="length")))
    client = build()
    with pytest.raises(ModelError, match="输出被截断"):
        client.complete_json("返回 JSON", "测试内容", task="extract")
    assert len(requests) == 1
    assert sleeps == []
    assert client.usage["model_calls"] == 1


def test_invalid_json_retries_at_most_twice_then_fails_clearly(mocked_client):
    build, requests, responses, sleeps, log_path = mocked_client
    # 第一轮不可解析，第二轮虽为合法 JSON，但根节点不是协议要求的对象。
    responses.extend([(200, completion("this is not JSON")), (200, completion("[1, 2, 3]"))])
    client = build(max_calls=10)
    with pytest.raises(ModelError, match="返回数据格式不正确"):
        client.complete_json("返回 JSON", "测试内容", task="extract")
    assert len(requests) == 2
    assert client.calls == client.usage["model_calls"] == 2
    assert sleeps == [1]
    assert client.usage["prompt_tokens"] == 24
    assert TEST_KEY not in log_path.read_text(encoding="utf-8")
