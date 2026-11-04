"""使用锁定官方SDK验证MCP诊断日志和远端校验边界。"""
import asyncio
import json
import logging
import time

import pytest

from app.errors import Fault
from test_mcp_gateway import INPUT, OUTPUT, configuration, context, gateway, mcp_server


def _rewrite_transport(monkeypatch, rewrite):
    import httpx2
    original = httpx2.AsyncHTTPTransport

    class RewriteTransport(httpx2.AsyncBaseTransport):
        def __init__(self, **kwargs):
            self.inner = original(**kwargs)

        async def handle_async_request(self, request):
            body = json.loads(request.content) if request.content else {}
            replacement = rewrite(body, request)
            if replacement is not None:
                return httpx2.Response(200, json=replacement, request=request)
            return await self.inner.handle_async_request(request)

        async def aclose(self):
            await self.inner.aclose()

    monkeypatch.setattr(httpx2, "AsyncHTTPTransport", RewriteTransport)


def test_malformed_rpc_cannot_log_bearer(tmp_path, mcp_server, monkeypatch, caplog):
    secret = "fictional-mcp-bearer-secret-9381"
    config = configuration(mcp_server)
    config["servers"]["demo"]["tenants"]["tenant_a"]["credential_env"] = "MCP_AUDIT_CREDENTIAL"
    monkeypatch.setenv("MCP_AUDIT_CREDENTIAL", secret)
    observed = []

    def rewrite(body, request):
        if body.get("method") == "tools/call":
            observed.append(request.headers.get("authorization"))
            return {"jsonrpc": "2.0", "id": body["id"], "result": secret}
        return None

    _rewrite_transport(monkeypatch, rewrite)
    gw, _ = gateway(tmp_path, config)
    with pytest.raises(Fault) as caught:
        asyncio.run(gw.execute("demo", "lookup", {"text": "sample"}, context=context(),
                    action_level="read", input_schema=INPUT, output_schema=OUTPUT))
    assert observed == ["Bearer " + secret]
    assert secret not in caught.value.message
    assert secret not in caplog.text, "Malformed remote JSON-RPC exposed bearer through SDK logs"


def test_successful_sdk_debug_logs_hide_payload_without_muting_other_contexts(tmp_path, mcp_server, caplog):
    caplog.set_level(logging.DEBUG, logger="mcp.client.streamable_http")
    private_text = "fictional-private-tool-payload-8237"
    gw, _ = gateway(tmp_path, configuration(mcp_server))
    result = asyncio.run(gw.execute(
        "demo", "lookup", {"text": private_text}, context=context(),
        action_level="read", input_schema=INPUT, output_schema=OUTPUT,
    ))
    assert result["data"]["summary"] == private_text
    assert private_text not in caplog.text
    logging.getLogger("mcp.client.streamable_http").warning("测试调用外的诊断保持可见")
    assert "测试调用外的诊断保持可见" in caplog.text


def test_remote_schema_cpu_is_bounded_by_tool_deadline(tmp_path, mcp_server, monkeypatch):
    config = configuration(mcp_server)
    config["servers"]["demo"]["tools"]["lookup"]["timeout_ms"] = 1000
    tool_calls = []
    listed = []

    def rewrite(body, request):
        if body.get("method") == "tools/list":
            listed.append(True)
            return {"jsonrpc": "2.0", "id": body["id"], "result": {"cacheScope": "private", "ttlMs": 0,
                "resultType": "complete", "tools": [{
                "name": "private_lookup", "description": "Untrusted remote schema",
                "inputSchema": {"type": "object", "properties": {
                    "text": {"type": "string", "pattern": "^(a+)+$"}}}}]}}
        if body.get("method") == "tools/call":
            tool_calls.append(body)
        return None

    _rewrite_transport(monkeypatch, rewrite)
    gw, _ = gateway(tmp_path, config)

    async def run():
        ticks = []
        async def heartbeat():
            while True:
                ticks.append(time.monotonic())
                await asyncio.sleep(0.005)
        beat = asyncio.create_task(heartbeat())
        await asyncio.sleep(0)
        started = time.monotonic()
        try:
            with pytest.raises(Fault) as rejected:
                await gw.execute("demo", "lookup", {"text": "a" * 26 + "!"}, context=context(),
                                 action_level="read", input_schema=INPUT, output_schema=OUTPUT)
            print("schema_probe_result=" + rejected.value.code + ": " + rejected.value.message)
        finally:
            elapsed = time.monotonic() - started
            ticks.append(time.monotonic())
            beat.cancel()
            try:
                await beat
            except asyncio.CancelledError:
                pass
        gap = max(b - a for a, b in zip(ticks, ticks[1:]))
        print(f"tool_deadline=1.000 elapsed={elapsed:.3f} max_event_loop_gap={gap:.3f}")
        assert listed
        assert rejected.value.code in {"TOOL_TIMEOUT", "SCHEMA_VALIDATION_FAILED"}
        assert not tool_calls
        # 取消后允许完成隔离进程回收，但事件循环不能被远端正则占住。
        assert elapsed < 8 and gap < 0.5, "远端Schema校验阻塞事件循环或未在启动、校验和回收预算内结束"

    asyncio.run(run())
