"""已完成调用、共享缓存和结果分页须遵守同一MCP实时读取门禁。"""
import asyncio
import json

import pytest

from app.errors import Fault
from app.main import create_app
from app.result_routes import authorize_result
from app.runtime import Gateway
from test_mcp_gateway import configuration, INPUT, OUTPUT


@pytest.mark.parametrize("change", ["removed", "malformed", "rotated", "drift"])
def test_all_reuse_paths_recheck_credentials_and_known_drift(tmp_path, monkeypatch, change):
    config = configuration()
    config["servers"]["demo"]["p2"] = {"catalog_digest": "0" * 64}
    config["servers"]["demo"]["tenants"]["tenant_a"]["credential_env"] = "MCP_REUSE_TEST_CREDENTIAL"
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setenv("AGENT_MCP_SERVERS", str(path))
    monkeypatch.setenv("MCP_REUSE_TEST_CREDENTIAL", "fictional-original-credential")
    monkeypatch.setenv("MCP_REUSE_RESULT_KEY", "73" * 32)
    folder = tmp_path / "scenarios" / "demo"
    folder.mkdir(parents=True)
    manifest = {"id": "demo", "name": "demo", "description": "测试", "example": "sample", "version": "1.0.0",
        "tools": [{"name": "lookup", "description": "测试", "action_level": "read", "permission": "demo.read",
            "input_schema": INPUT, "output_schema": OUTPUT, "adapter": {"type": "mcp", "server_id": "demo", "tool_id": "lookup"}}]}
    (folder / "scenario.json").write_text(json.dumps(manifest), encoding="utf-8")
    actor = {"tenant_id": "tenant_a", "org_id": "org", "user_id": "user", "grants": {"demo": {"permissions": ["demo.read"]}}}
    settings = {"p2": {"read_cache": {"tools": {"demo/lookup": {"ttl_s": 60, "freshness": "v1"}}},
                       "results": {"keys": {"local": "MCP_REUSE_RESULT_KEY"}, "active_key": "local"}}}
    app = create_app(scenarios=folder.parent, database=tmp_path / "agent.db", principals={"reader": actor}, model_settings=settings)
    async def run():
        async with app.router.lifespan_context(app):
            runtime = app.state.runtime
            remote = runtime.catalog._mcp_gateway
            calls = []
            def gateway():
                task, _ = runtime.store.start(actor, {"business_id": "demo", "mode": "mock", "message": "sample"}, None, "1.0.0",
                                              snapshot_id=app.state.snapshots.baseline("demo"))
                return Gateway(runtime, runtime.store.context_id(task["task_id"]))
            async def execute(*args, **kwargs):
                calls.append(1)
                return {"data": {"summary": "受保护结果"}, "evidence": []}
            monkeypatch.setattr(remote, "_execute", execute)
            first = gateway()
            await first.execute("lookup", {"text": "sample"}, "read")
            ref = runtime.store._db.execute("SELECT result_ref FROM tool_results WHERE task_id=?", (first.task_id,)).fetchone()[0]
            if change == "removed":
                monkeypatch.delenv("MCP_REUSE_TEST_CREDENTIAL")
            elif change == "malformed":
                monkeypatch.setenv("MCP_REUSE_TEST_CREDENTIAL", "bad\nheader")
            elif change == "rotated":
                monkeypatch.setenv("MCP_REUSE_TEST_CREDENTIAL", "fictional-new-credential")
            else:
                async def drift(*args, **kwargs):
                    raise Fault("MCP_CATALOG_DRIFT", "测试已发现目录漂移", 502)
                monkeypatch.setattr(remote, "_execute", drift)
                with pytest.raises(Fault):
                    await gateway().execute("lookup", {"text": "different"}, "read")
            with pytest.raises(Fault):
                await first.execute("lookup", {"text": "sample"}, "read")
            with pytest.raises(Fault):
                runtime.result_store.metadata(ref, actor, lambda meta, source: authorize_result(runtime, actor, meta, source))
            if change != "rotated":
                with pytest.raises(Fault):
                    await gateway().execute("lookup", {"text": "sample"}, "read")
            assert calls == [1]
    asyncio.run(run())
