"""P2目录批准和健康策略使用真实协议以及可控故障验证。"""
import asyncio
import json
import time
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.errors import Fault
from app.mcp_gateway import MCPGateway, catalog_digest
from test_mcp_gateway import INPUT, OUTPUT, configuration, context, gateway, call, mcp_server


def policy_config(url="http://127.0.0.1:8123/mcp"):
    config = configuration(url)
    config["servers"]["demo"]["p2"] = {"catalog_digest": "0" * 64,
        "failure_threshold": 2, "cooldown_s": 0.05, "max_concurrency": 1, "rate_per_minute": 120,
        "cache_ttl_s": 30}
    return config


def test_actual_directory_candidate_requires_explicit_approval(tmp_path, mcp_server):
    config = policy_config(mcp_server)
    gw, path = gateway(tmp_path, config)
    candidate = asyncio.run(gw.inspect_catalog("demo", "lookup", context=context()))
    assert candidate["catalog_digest"] != "0" * 64 and len(candidate["tools"]) == 1
    assert json.loads(path.read_text())["servers"]["demo"]["p2"]["catalog_digest"] == "0" * 64
    with pytest.raises(Fault) as denied:
        call(gw, context())
    assert denied.value.code == "MCP_CATALOG_DRIFT"
    config["servers"]["demo"]["p2"]["catalog_digest"] = candidate["catalog_digest"]
    path.write_text(json.dumps(config), encoding="utf-8")
    assert call(gw, context())["data"]["summary"] == "sample"


@pytest.mark.parametrize("field", ["description", "inputSchema", "outputSchema", "name"])
def test_catalog_digest_detects_all_reviewed_fields(field):
    tools = [{"name": "lookup", "description": "原描述", "inputSchema": INPUT, "outputSchema": OUTPUT}]
    before = catalog_digest(tools)
    changed = json.loads(json.dumps(tools))
    changed[0][field] = "变化" if field in {"name", "description"} else {"type": "object"}
    assert before != catalog_digest(changed)
    assert before != catalog_digest(tools + [{**tools[0], "name": "new"}])


def test_circuit_has_one_probe_and_cancel_releases_slot(tmp_path, monkeypatch):
    gw, _ = gateway(tmp_path, policy_config())
    calls = []
    async def fail(*args, **kwargs):
        calls.append(1)
        raise Fault("UPSTREAM_UNAVAILABLE", "测试上游故障", 503, True)
    monkeypatch.setattr(gw, "_execute", fail)
    for _ in range(2):
        with pytest.raises(Fault):
            call(gw, context())
    with pytest.raises(Fault) as opened:
        call(gw, context())
    assert opened.value.code == "MCP_CIRCUIT_OPEN" and len(calls) == 2
    async def run():
        await asyncio.sleep(0.06)
        entered = asyncio.Event()
        async def blocked(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        monkeypatch.setattr(gw, "_execute", blocked)
        one = asyncio.create_task(gw.execute("demo", "lookup", {}, context=context(), action_level="read", input_schema=INPUT, output_schema=OUTPUT))
        await entered.wait()
        with pytest.raises(Fault) as busy:
            await gw.execute("demo", "lookup", {}, context=context(), action_level="read", input_schema=INPUT, output_schema=OUTPUT)
        assert busy.value.code == "MCP_CIRCUIT_OPEN"
        one.cancel()
        with pytest.raises(asyncio.CancelledError):
            await one
        assert all(state["inflight"] == 0 for state in gw.health.values())
    asyncio.run(run())


def test_protocol_incompatible_does_not_heal_without_config_change(tmp_path, monkeypatch):
    gw, path = gateway(tmp_path, policy_config())
    calls = []
    async def fail(*args, **kwargs):
        calls.append(1)
        raise Fault("MCP_PROTOCOL_INCOMPATIBLE", "测试协议不兼容", 502)
    monkeypatch.setattr(gw, "_execute", fail)
    for _ in range(2):
        with pytest.raises(Fault):
            call(gw, context())
    assert len(calls) == 1
    config = policy_config()
    config["servers"]["demo"]["tools"]["lookup"]["version"] = "1.0.1"
    path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(Fault):
        call(gw, context())
    assert len(calls) == 2


def test_rate_limit_and_health_are_scoped_by_identity(tmp_path, monkeypatch):
    config = policy_config()
    config["servers"]["demo"]["p2"]["rate_per_minute"] = 1
    gw, _ = gateway(tmp_path, config)
    async def success(*args, **kwargs):
        return {"data": {"summary": "ok"}, "evidence": []}
    monkeypatch.setattr(gw, "_execute", success)
    assert call(gw, context())["data"]["summary"] == "ok"
    with pytest.raises(Fault) as denied:
        call(gw, context())
    assert denied.value.code == "RATE_LIMITED"
    assert call(gw, context("tenant_b"))["data"]["summary"] == "ok"


def test_directory_ttl_and_public_hint_never_share_tenants(tmp_path):
    config = policy_config()
    tools = [{"name": "lookup", "description": "测试", "inputSchema": INPUT, "outputSchema": OUTPUT}]
    server = config["servers"]["demo"]
    server["p2"]["catalog_digest"] = catalog_digest(tools)
    gw, _ = gateway(tmp_path, config)
    listing = SimpleNamespace(tools=[SimpleNamespace(name="lookup", description="测试", input_schema=INPUT, output_schema=OUTPUT)],
                              ttl_ms=1000000, cache_scope="public")
    endpoint = config["endpoints"]["local"]
    for tenant in ("tenant_a", "tenant_b"):
        gw._approve_listing(listing, endpoint, server, context(tenant), "demo", False)
    assert len(gw.directories) == 2
    assert all(item["scope"] == "private" and item["expires_at"] <= time.monotonic() + 30 for item in gw.directories.values())
    listing.ttl_ms = 0
    gw._approve_listing(listing, endpoint, server, context(), "demo", False)
    assert len(gw.directories) == 1


@pytest.mark.parametrize("protocol", ["2024-11-05", "2025-03-26", "2025-06-18"])
def test_unapproved_legacy_protocols_are_explicitly_rejected(tmp_path, protocol):
    config = policy_config()
    config["servers"]["demo"]["approved_protocol_versions"] = [protocol]
    gw, _ = gateway(tmp_path, config)
    with pytest.raises(Fault) as denied:
        call(gw, context())
    assert denied.value.code == "MCP_PROTOCOL_INCOMPATIBLE"


def test_stdio_configuration_is_rejected_without_spawning(tmp_path):
    config = configuration()
    config["endpoints"]["local"] = {"command": sys.executable, "args": ["untrusted.py"], "transport": "stdio"}
    with pytest.raises(ValueError):
        gateway(tmp_path, config)


def test_operator_cli_outputs_candidate_without_modifying_approval(tmp_path, mcp_server):
    config = policy_config(mcp_server)
    _, path = gateway(tmp_path, config)
    output = tmp_path / "candidate.json"
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, str(root / "scripts/mcpctl.py"), "--config", str(path),
                             "--server", "demo", "--tool", "lookup", "--tenant", "tenant_a", "--output", str(output)],
                            cwd=root, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    candidate = json.loads(output.read_text(encoding="utf-8"))
    assert not candidate["matches_approval"] and candidate["tools"]
    assert json.loads(path.read_text(encoding="utf-8")) == config


def test_structure_bound_precedes_sdk_output_schema(tmp_path, mcp_server, monkeypatch):
    from mcp.client.session import ClientSession
    original = ClientSession.validate_tool_result
    oversized_validations = []
    async def validate(self, name, result):
        if result.structured_content and len(result.structured_content.get("evidence", [])) >= 10001:
            oversized_validations.append(name)
        return await original(self, name, result)
    monkeypatch.setattr(ClientSession, "validate_tool_result", validate)
    gw, _ = gateway(tmp_path, configuration(mcp_server))
    with pytest.raises(Fault) as denied:
        call(gw, context(), {"text": "many_nodes"})
    assert denied.value.code == "TOOL_OUTPUT_INVALID"
    assert not oversized_validations


def test_gateway_mcp_shared_reads_pin_every_task(tmp_path, mcp_server, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from app.runtime import Gateway
    config = policy_config(mcp_server)
    gw, path = gateway(tmp_path, config)
    candidate = asyncio.run(gw.inspect_catalog("demo", "lookup", context=context()))
    config["servers"]["demo"]["p2"]["catalog_digest"] = candidate["catalog_digest"]
    path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setenv("AGENT_MCP_SERVERS", str(path))
    root = tmp_path / "scenarios"
    folder = root / "demo"
    folder.mkdir(parents=True)
    manifest = {"id": "demo", "name": "demo", "description": "测试", "example": "sample", "version": "1.0.0",
        "tools": [{"name": "lookup", "description": "测试工具", "action_level": "read", "permission": "demo.read",
            "input_schema": INPUT, "output_schema": OUTPUT, "adapter": {"type": "mcp", "server_id": "demo", "tool_id": "lookup"}}]}
    (folder / "scenario.json").write_text(json.dumps(manifest), encoding="utf-8")
    actor = {"tenant_id": "tenant_a", "org_id": "org", "user_id": "user", "grants": {"demo": {"permissions": ["demo.read"]}}}
    settings = {"p2": {"read_cache": {"tools": {"demo/lookup": {"ttl_s": 60, "freshness": "v1"}}}}}
    app = create_app(scenarios=root, database=tmp_path / "agent.db", principals={"test-reader": actor}, model_settings=settings)
    with TestClient(app, base_url="http://localhost"):
        runtime = app.state.runtime
        physical = []
        remote_gateway = runtime.catalog._mcp_gateway
        execute = remote_gateway.execute
        async def count(*args, **kwargs):
            physical.append(1)
            return await execute(*args, **kwargs)
        monkeypatch.setattr(remote_gateway, "execute", count)
        async def run():
            gateways = []
            for _ in range(3):
                task, _ = runtime.store.start(actor, {"business_id": "demo", "mode": "mock", "message": "sample"}, None, "1.0.0")
                gateways.append(Gateway(runtime, runtime.store.context_id(task["task_id"])))
            results = await asyncio.gather(*[item.execute("lookup", {"text": "sample"}, "read") for item in gateways])
            assert len(physical) == 1 and len({item["execution_id"] for item in results}) == 3
            assert runtime.store._db.execute("SELECT COUNT(*) FROM task_mcp_bindings").fetchone()[0] == 3
            config["servers"]["demo"]["enabled"] = False
            path.write_text(json.dumps(config), encoding="utf-8")
            with pytest.raises(Fault) as denied:
                await gateways[0].execute("lookup", {"text": "sample"}, "read")
            assert denied.value.code == "CAPABILITY_REVOKED" and len(physical) == 1
        asyncio.run(run())
