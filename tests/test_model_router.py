"""可信画像、请求边界切换与模型协议的隔离测试。"""
import asyncio
import copy
import importlib
import importlib.util
import json
import time
from types import SimpleNamespace

import httpx
import pytest

from app.errors import Fault


def policy():
    profiles = {}
    for index, name in enumerate(("primary", "secondary")):
        profiles[name] = {
            "provider_id": name, "model_ref": "fictional-model", "base_url": f"http://127.0.0.1:{18881 + index}/v1",
            "credential_env": "", "enabled": True, "profile_version": "test-1",
            "verified_at": time.time() - 30, "expires_at": time.time() + 3600,
            "verification_kind": "mock", "evidence_digest": "a" * 64,
            "supports_tool_calling": True, "supports_structured_output": True, "supports_streaming": True,
            "supports_parallel_tool_calls": False, "supports_usage_reporting": True,
            "context_window_tokens": 32000, "max_output_tokens": 128,
            "data_handling_class": ["synthetic"], "region": "local", "retention_class": "no_store",
            "input_usd_per_million": 1.0, "output_usd_per_million": 2.0,
        }
    return {"version": "1", "profiles": profiles, "routes": {"notes": {
        "allowed_providers": ["primary", "secondary"], "data_class": "synthetic",
        "allowed_regions": ["local"], "allowed_retention_classes": ["no_store"],
        "result_max_age_s": 60, "stream": False, "max_attempts": 2,
        "max_total_tokens": 100000, "max_cost_usd": 1.0, "timeout_s": 2,
    }}}


def router_module():
    assert importlib.util.find_spec("app.model_router") is not None, "P3模型路由尚未实现"
    return importlib.import_module("app.model_router")


def gateway():
    rows = []
    return SimpleNamespace(context=lambda: {"business_id": "notes", "task_id": "task", "tenant_id": "t"},
                           task_id="task", store=SimpleNamespace(audit=lambda *a, **k: rows.append((a, k))),
                           audit_rows=rows)


def response(answer="完成", **extra):
    message = {"role": "assistant", "content": json.dumps({"answer": answer, "limitations": [], "unresolved_items": []})}
    return {"choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}, **extra}


def make_router(config, handler):
    settings = {"provider_policy": config, "transport": httpx.MockTransport(handler)}
    return router_module().ProviderRouter(settings, gateway(), []), settings


def complete(router):
    return asyncio.run(router.complete([{"role": "user", "content": "虚构测试"}], None, lambda: None))


@pytest.mark.parametrize("field,value", [
    ("supports_tool_calling", "unknown"), ("supports_structured_output", False),
    ("expires_at", 1), ("enabled", False), ("region", "elsewhere"),
    ("data_handling_class", ["public"]), ("retention_class", "stores_forever"),
])
def test_incompatible_profiles_never_receive_input(field, value):
    config = policy()
    for profile in config["profiles"].values():
        profile[field] = value
    sent = []
    router, _ = make_router(config, lambda request: sent.append(request))
    with pytest.raises(Fault) as rejected:
        complete(router)
    assert rejected.value.code == "MODEL_CAPABILITY_UNAVAILABLE"
    assert not sent


def test_unknown_policy_fields_and_duplicate_json_are_rejected(tmp_path, monkeypatch):
    module = router_module()
    config = policy()
    config["extra"] = True
    with pytest.raises(Fault):
        module.load_policy({"provider_policy": config})
    path = tmp_path / "policy.json"
    path.write_text('{"version":"1","version":"2"}', encoding="utf-8")
    monkeypatch.setenv("AGENT_PROVIDER_POLICY", str(path))
    with pytest.raises(Fault):
        module.load_policy({})


def test_strict_request_and_known_usage():
    observed = []
    def handler(request):
        observed.append(json.loads(request.content))
        return httpx.Response(200, json=response())
    router, _ = make_router(policy(), handler)
    reply, usage = complete(router)
    assert usage["total_tokens"] == 15 and reply["role"] == "assistant"
    assert observed[0]["response_format"]["json_schema"]["strict"] is True
    assert observed[0]["max_completion_tokens"] == 128
    assert observed[0]["parallel_tool_calls"] is False


@pytest.mark.parametrize("status", [429, 503, 408])
def test_failed_attempt_switches_once_and_retains_unknown_usage(status):
    ports = []
    def handler(request):
        ports.append(request.url.port)
        return httpx.Response(status) if len(ports) == 1 else httpx.Response(200, json=response())
    router, _ = make_router(policy(), handler)
    reply, usage = complete(router)
    assert reply["content"] and usage is None
    assert ports == [18881, 18882] and router.generation == 1
    assert router.reserved_tokens > 15


def test_changed_policy_and_revoked_credentials_reject_before_send(monkeypatch):
    config = policy()
    config["profiles"]["primary"]["credential_env"] = "P3_TEST_KEY"
    monkeypatch.setenv("P3_TEST_KEY", "fictional-key")
    sent = []
    router, settings = make_router(config, lambda req: sent.append(req) or httpx.Response(200, json=response()))
    complete(router)
    monkeypatch.delenv("P3_TEST_KEY")
    with pytest.raises(Fault):
        complete(router)
    assert len(sent) == 1
    settings["provider_policy"]["version"] = "changed"
    with pytest.raises(Fault):
        complete(router)
    assert len(sent) == 1


def test_budget_blocks_attempt_before_transmitting():
    config = policy()
    config["routes"]["notes"]["max_total_tokens"] = 1
    sent = []
    router, _ = make_router(config, lambda req: sent.append(req))
    with pytest.raises(Fault) as rejected:
        complete(router)
    assert rejected.value.code == "BUDGET_EXCEEDED" and not sent


@pytest.mark.parametrize("change", ["refusal", "multi_choice", "bad_json", "length"])
def test_invalid_protocol_does_not_trigger_fallback(change):
    body = response()
    if change == "refusal":
        body["choices"][0]["message"]["refusal"] = "拒绝"
    elif change == "multi_choice":
        body["choices"].append(copy.deepcopy(body["choices"][0]))
    elif change == "bad_json":
        body["choices"][0]["message"]["content"] = '{"answer":"a","answer":"b"}'
    else:
        body["choices"][0]["finish_reason"] = "length"
    calls = []
    router, _ = make_router(policy(), lambda req: calls.append(req) or httpx.Response(200, json=body))
    with pytest.raises(Fault):
        complete(router)
    assert len(calls) == 1


def sse(body, done=True):
    choice = body["choices"][0]
    chunks = [{"choices": [{"index": 0, "delta": choice["message"], "finish_reason": None}]},
              {"choices": [{"index": 0, "delta": {}, "finish_reason": choice["finish_reason"]}]},
              {"choices": [], "usage": body["usage"]}]
    text = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
    if done:
        text += "data: [DONE]\n\n"
    return httpx.Response(200, content=text, headers={"content-type": "text/event-stream"})


def test_incomplete_stream_cannot_publish_partial_result():
    config = policy()
    config["routes"]["notes"]["stream"] = True
    calls = []
    def handler(request):
        calls.append(request)
        return sse(response("未完成" if len(calls) == 1 else "完整"), done=len(calls) > 1)
    router, _ = make_router(config, handler)
    reply, usage = complete(router)
    assert json.loads(reply["content"])["answer"] == "完整"
    assert len(calls) == 2 and usage is None


def test_cancelled_request_never_tries_another_provider():
    calls = []
    async def handler(request):
        calls.append(request)
        raise asyncio.CancelledError()
    router, _ = make_router(policy(), handler)
    with pytest.raises(asyncio.CancelledError):
        complete(router)
    assert len(calls) == 1


def test_total_request_timeout_also_bounds_a_slow_transport():
    config = policy()
    config["routes"]["notes"]["timeout_s"] = 0.02
    ports = []
    async def handler(request):
        ports.append(request.url.port)
        if request.url.port == 18881:
            await asyncio.sleep(0.1)
        return httpx.Response(200, json=response())
    router, _ = make_router(config, handler)
    complete(router)
    assert ports == [18881, 18882]


def test_json_escaped_credential_cannot_leave_the_provider(monkeypatch):
    config = policy()
    config["profiles"]["primary"]["credential_env"] = "P3_TEST_KEY"
    monkeypatch.setenv("P3_TEST_KEY", "fictional-key\\with-escape")
    router, _ = make_router(config, lambda req: httpx.Response(200, json=response("fictional-key\\with-escape")))
    with pytest.raises(Fault):
        complete(router)
