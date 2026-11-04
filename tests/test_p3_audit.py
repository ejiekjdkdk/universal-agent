"""P3独立审查发现的问题必须先复现，再验证拒绝边界。"""
import asyncio
import copy
import json
from pathlib import Path

import httpx
import pytest

from app.errors import Fault
from app import replay
from test_model_router import policy, response, make_router, complete
from test_replay import case, KEY


@pytest.mark.parametrize("change", ["calls_with_stop", "final_with_tools"])
def test_finish_reason_must_match_payload_before_execution(change):
    value = case()
    if change == "calls_with_stop":
        value["responses"][0]["body"]["choices"][0]["finish_reason"] = "stop"
    else:
        value["responses"] = value["responses"][1:]
        value["responses"][0]["body"]["choices"][0]["finish_reason"] = "tool_calls"
    result = replay._execute(value)
    assert result["status"] == "failed" and result["error"]["code"] == "MODEL_INVALID_RESPONSE"
    assert result["tool_calls"] == []


def test_replay_ignores_unrelated_live_service_credentials(tmp_path, monkeypatch):
    path = tmp_path / "case.zip"
    replay.record_case(case(), path, KEY)
    monkeypatch.setenv("AGENT_ADMIN_TOKEN", "虚构结果")
    monkeypatch.setenv("AGENT_SERVICE_TOKEN", "虚构结果")
    assert replay.run_replay(path, KEY)["status"] == "PASS"


def test_engine_digest_covers_execution_dependencies(monkeypatch):
    original = Path.read_bytes
    before = replay._engine_digest()
    def changed(path):
        value = original(path)
        if path.name == "schema_validation.py":
            return value + b"\n# simulated dependency change\n"
        return value
    monkeypatch.setattr(Path, "read_bytes", changed)
    assert replay._engine_digest() != before


@pytest.mark.parametrize("field", ["version", "source_id", "extension_sha256", "library_sha256"])
def test_replay_rejects_native_sqlite_change(tmp_path, monkeypatch, field):
    """更换原生库而不改 Python/pip 时，也不能误报为精确回放。"""
    native = {"version": "3.53.1", "source_id": "fictional-build-a", "threadsafety": 3,
              "extension_sha256": "a" * 64, "library_sha256": "b" * 64}
    monkeypatch.setattr(replay, "sqlite_runtime", lambda: dict(native), raising=False)
    path = tmp_path / "native.zip"
    replay.record_case(case(), path, KEY)
    assert replay.run_replay(path, KEY)["status"] == "PASS"
    native[field] = "fictional-changed-sqlite"
    with pytest.raises(ValueError, match="运行代码"):
        replay.run_replay(path, KEY)


def test_custom_profile_secret_cannot_enter_tool_store_or_model(monkeypatch):
    secret = 'fictional-custom-provider-key-987\\escaped'
    monkeypatch.setenv("P3_AUDIT_KEY", secret)
    original = replay._policy
    def configured(value):
        config = original(value)
        config["profiles"]["recorded"]["credential_env"] = "P3_AUDIT_KEY"
        # 此测试直接运行夹具以检验真实Gateway，不固定未来时间。
        import time
        config["profiles"]["recorded"]["verified_at"] = time.time() - 1
        config["profiles"]["recorded"]["expires_at"] = time.time() + 3600
        return config
    # 配置必须冻结，否则本测试会先触发策略变更门禁。
    config = configured(case())
    monkeypatch.setattr(replay, "_policy", lambda value: copy.deepcopy(config))
    value = case()
    value["tools"][0]["recordings"][0]["output"]["data"]["summary"] = secret
    result = asyncio.run(replay._run_case(value))
    assert result["status"] == "failed" and result["error"]["code"] == "SENSITIVE_OUTPUT"
    assert sum(item["kind"] == "model" for item in result["requests"]) == 1
    assert secret not in json.dumps(result)


def test_reply_cannot_echo_another_candidate_credential(monkeypatch):
    secret = "fictional-secondary-credential-123456"
    monkeypatch.setenv("P3_SECONDARY_KEY", secret)
    config = policy()
    config["profiles"]["secondary"]["credential_env"] = "P3_SECONDARY_KEY"
    router, _ = make_router(config, lambda request: httpx.Response(200, json=response(secret)))
    with pytest.raises(Fault):
        complete(router)


def test_cancelled_attempt_preserves_unknown_usage_audit():
    async def cancel(request):
        raise asyncio.CancelledError()
    router, _ = make_router(policy(), cancel)
    with pytest.raises(asyncio.CancelledError):
        complete(router)
    assert router.requests == 1 and router.reserved_tokens > 0
    assert len(router.gateway.audit_rows) == 1
    metadata = json.loads(router.gateway.audit_rows[0][0][-1])
    assert metadata["outcome"] == "cancelled" and metadata["usage_known"] is False


def test_tool_schema_cannot_send_a_profile_credential(monkeypatch):
    secret = "fictional-tool-schema-provider-key-987"
    monkeypatch.setenv("P3_SECONDARY_KEY", secret)
    config = policy()
    config["profiles"]["secondary"]["credential_env"] = "P3_SECONDARY_KEY"
    sent = []
    router, _ = make_router(config, lambda request: sent.append(request) or httpx.Response(200, json=response()))
    router.tools = [{"type": "function", "function": {"name": "sample", "description": secret,
                    "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}]
    with pytest.raises(Fault):
        complete(router)
    assert not sent
