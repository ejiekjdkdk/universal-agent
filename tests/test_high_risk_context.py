"""已有工具结果再次进入模型或上下文引用时，必须重新检查来源授权。"""
import asyncio
from functools import partial
import json

import httpx
import pytest

from app.context_compiler import ContextSession
from app.errors import Fault
from app.main import create_app
from app.providers import run_provider
from app.runtime import Gateway
from test_mcp_gateway import INPUT, OUTPUT, configuration


@pytest.fixture
def context_app(tmp_path, monkeypatch):
    config = configuration()
    config["servers"]["demo"]["p2"] = {"catalog_digest": "0" * 64}
    config["servers"]["demo"]["tenants"]["tenant_a"]["credential_env"] = "CONTEXT_TEST_MCP_KEY"
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setenv("AGENT_MCP_SERVERS", str(path))
    monkeypatch.setenv("CONTEXT_TEST_MCP_KEY", "fictional-context-original")
    folder = tmp_path / "scenarios" / "demo"
    folder.mkdir(parents=True)
    manifest = {
        "id": "demo", "name": "测试", "description": "上下文授权测试", "example": "sample", "version": "1.0.0",
        "tools": [{
            "name": "lookup", "description": "测试读取", "action_level": "read", "permission": "demo.read",
            "input_schema": INPUT, "output_schema": OUTPUT,
            "adapter": {"type": "mcp", "server_id": "demo", "tool_id": "lookup"},
        }],
    }
    (folder / "scenario.json").write_text(json.dumps(manifest), encoding="utf-8")
    actor = {
        "tenant_id": "tenant_a", "org_id": "org", "user_id": "user",
        "grants": {"demo": {"permissions": ["demo.read"]}},
    }
    app = create_app(scenarios=folder.parent, database=tmp_path / "agent.db", principals={"reader": actor}, model_settings={})
    return app, actor


def new_gateway(app, actor):
    runtime = app.state.runtime
    task, _ = runtime.store.start(
        actor, {"business_id": "demo", "mode": "openai", "message": "sample"}, None, "1.0.0",
        snapshot_id=app.state.snapshots.baseline("demo"),
    )
    return Gateway(runtime, runtime.store.context_id(task["task_id"]))


async def change_authority(app, actor, monkeypatch, change):
    runtime = app.state.runtime
    if change == "removed":
        monkeypatch.delenv("CONTEXT_TEST_MCP_KEY")
    elif change == "rotated":
        monkeypatch.setenv("CONTEXT_TEST_MCP_KEY", "fictional-context-rotated")
    elif change == "capability":
        runtime.releases.revoke_capability("demo", "lookup", actor=actor)
    elif change == "drift":
        async def report_drift(*args, **kwargs):
            raise Fault("MCP_CATALOG_DRIFT", "测试目录变更", 502)
        monkeypatch.setattr(runtime.catalog._mcp_gateway, "_execute", report_drift)
        with pytest.raises(Fault, match="测试目录变更"):
            await new_gateway(app, actor).execute("lookup", {"text": "other"}, "other")


async def physical_read(remote, server_id, tool_id, arguments, **kwargs):
    # 只替换远端传输；真实网关仍负责权限、配置固定、健康状态与调用持久化。
    context = kwargs["context"]
    endpoint, server, tool = remote._resolve(server_id, tool_id, context)
    context.pin_configuration(server_id + "/" + tool_id, remote.binding_digest(endpoint, server, tool, context))
    return {"data": {"summary": "受保护的测试结果"}, "evidence": []}


@pytest.mark.parametrize("change", ["removed", "rotated", "drift"])
@pytest.mark.parametrize("operation", ["compile", "read"])
def test_context_reuse_rechecks_mcp_authority(context_app, monkeypatch, change, operation):
    app, actor = context_app

    async def run():
        async with app.router.lifespan_context(app):
            remote = app.state.runtime.catalog._mcp_gateway
            monkeypatch.setattr(remote, "_execute", partial(physical_read, remote))
            gateway = new_gateway(app, actor)
            result = await gateway.execute("lookup", {"text": "sample"}, "first")
            session = ContextSession(gateway, {}, [])
            ref = session.add(
                {"role": "tool", "tool_call_id": "first", "content": json.dumps(result)},
                "tool_result", True, capability="lookup", arguments={"text": "sample"},
            )
            assert session.read(ref) and session.compile().messages_json
            await change_authority(app, actor, monkeypatch, change)
            with pytest.raises(Fault):
                if operation == "read":
                    session.read(ref)
                else:
                    session.compile()
    asyncio.run(run())


@pytest.mark.parametrize("compiler_enabled", [False, True])
@pytest.mark.parametrize("change", ["unchanged", "removed", "rotated", "drift", "capability"])
def test_model_round_does_not_resend_revoked_tool_data(context_app, monkeypatch, compiler_enabled, change):
    app, actor = context_app
    requests = []

    def upstream(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            reply = {"role": "assistant", "tool_calls": [{
                "id": "first", "type": "function", "function": {
                    "name": "execute_capability",
                    "arguments": json.dumps({"name": "lookup", "arguments": {"text": "sample"}}),
                },
            }]}
        else:
            reply = {"role": "assistant", "content": "完成"}
        return httpx.Response(200, json={"choices": [{"message": reply}]})

    async def run():
        async with app.router.lifespan_context(app):
            remote = app.state.runtime.catalog._mcp_gateway
            monkeypatch.setattr(remote, "_execute", partial(physical_read, remote))
            gateway = new_gateway(app, actor)
            execute = gateway.execute

            async def revoke_after_read(*args, **kwargs):
                result = await execute(*args, **kwargs)
                await change_authority(app, actor, monkeypatch, change)
                return result

            monkeypatch.setattr(gateway, "execute", revoke_after_read)
            settings = {
                "base_url": "http://localhost", "model": "test", "transport": httpx.MockTransport(upstream),
                "context_compiler": compiler_enabled,
            }
            pending = run_provider("openai", {}, "sample", [], gateway, settings)
            if change == "unchanged":
                assert await pending == "完成"
                assert len(requests) == 2
                assert "受保护的测试结果" in json.dumps(requests[1], ensure_ascii=False)
            else:
                with pytest.raises(Fault):
                    await pending
                assert len(requests) == 1
    asyncio.run(run())
