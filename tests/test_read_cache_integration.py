"""经过真实Gateway的缓存命中仍有独立调用记录和实时权限检查。"""
import asyncio

from fastapi.testclient import TestClient
import pytest

from app.errors import Fault
from app.main import create_app
from app.runtime import Gateway


BODY = {"business_id": "notes", "message": "会议纪要", "mode": "mock"}


def application(tmp_path):
    policy = {"tools": {"notes/notes_search": {"ttl_s": 60, "freshness": "notes-v1"}}}
    return create_app(database=tmp_path / "cache.db", token="reader", model_settings={"p2": {"read_cache": policy}})


def gateway(runtime, actor=None):
    actor = actor or runtime.principals["reader"]
    task, _ = runtime.store.start(actor, BODY, None, "1.0.1")
    return Gateway(runtime, runtime.store.context_id(task["task_id"]))


def test_shared_query_has_independent_results_and_does_not_hold_waiter_slots(tmp_path):
    app = application(tmp_path)
    with TestClient(app, base_url="http://localhost"):
        runtime = app.state.runtime
        calls = []
        async def handler(arguments, retrieve):
            calls.append(arguments)
            await asyncio.sleep(0.1)
            return {"data": {"summary": "完成", "matches": 1}, "evidence": retrieve(arguments["text"], 5)}
        runtime.catalog._handlers[("notes", "notes_search")] = handler
        async def run():
            runtime.slots = asyncio.Semaphore(1)
            gateways = [gateway(runtime) for _ in range(10)]
            results = await asyncio.wait_for(asyncio.gather(*[
                item.execute("notes_search", {"text": "会议纪要"}, "read") for item in gateways]), 3)
            assert len(calls) == 1
            assert len({item["execution_id"] for item in results}) == 10
            assert all(item["data"]["summary"] == "完成" for item in results)
            assert runtime.store._db.execute("SELECT COUNT(*) FROM calls WHERE status='completed'").fetchone()[0] == 10
            assert runtime.store._db.execute("SELECT SUM(count) FROM task_tool_attempts").fetchone()[0] == 1
            results[0]["data"]["summary"] = "修改"
            assert results[1]["data"]["summary"] == "完成"
        asyncio.run(run())


@pytest.mark.parametrize("changed", ["arguments", "version", "permissions", "scope", "skill"])
def test_cache_key_binds_authority_and_input(tmp_path, changed):
    app = application(tmp_path)
    with TestClient(app, base_url="http://localhost"):
        runtime = app.state.runtime
        item = gateway(runtime)
        tool = item.catalog.discover("notes")[0]
        arguments = {"text": "会议纪要"}
        before = item._cache_scope(tool, arguments)
        if changed == "arguments":
            arguments["text"] = "其他资料"
        elif changed == "version":
            tool["version"] = "2.0.0"
        elif changed == "permissions":
            runtime.principals["reader"]["grants"]["notes"]["permissions"].append("extra.read")
        elif changed == "scope":
            runtime.principals["reader"]["grants"]["notes"].setdefault("objects", []).append("different")
            runtime.store.scope = lambda task_id: {"object_ids": ["different"]}
        else:
            item.pin = {"name": "another"}
            item.context = lambda: runtime.store.run(item.run_id)
        assert item._cache_scope(tool, arguments) != before


@pytest.mark.parametrize("kind", ["propose_write", "agent", "unconfigured"])
def test_write_agent_and_unconfigured_tool_never_enter_cache(tmp_path, kind):
    app = application(tmp_path)
    with TestClient(app, base_url="http://localhost"):
        runtime = app.state.runtime
        item = gateway(runtime)
        tool = item.catalog.discover("notes")[0]
        if kind == "propose_write":
            tool["action_level"] = kind
        elif kind == "agent":
            item.catalog._tools["notes"][tool["name"]]["adapter"]["type"] = kind
        else:
            runtime.read_cache.policies.clear()
        assert item._cache_scope(tool, {"text": "会议纪要"}) is None


def test_cache_hit_still_enforces_logical_call_budget(tmp_path):
    app = application(tmp_path)
    with TestClient(app, base_url="http://localhost"):
        runtime = app.state.runtime
        async def run():
            item = gateway(runtime)
            for number in range(8):
                await item.execute("notes_search", {"text": "会议纪要"}, str(number))
            with pytest.raises(Fault) as denied:
                await item.execute("notes_search", {"text": "会议纪要"}, "ninth")
            assert denied.value.code == "RATE_LIMITED"
            assert runtime.store._db.execute("SELECT SUM(count) FROM task_tool_attempts").fetchone()[0] == 1
        asyncio.run(run())


def test_leader_cancellation_preserves_follower_authorized_retrieval(tmp_path):
    app = application(tmp_path)
    with TestClient(app, base_url="http://localhost"):
        runtime = app.state.runtime
        async def run():
            started, finish = asyncio.Event(), asyncio.Event()
            calls = []
            async def handler(arguments, retrieve):
                calls.append(1)
                started.set()
                await finish.wait()
                return {"data": {"summary": "完成", "matches": 1}, "evidence": retrieve("会议纪要", 5)}
            runtime.catalog._handlers[("notes", "notes_search")] = handler
            first, second = gateway(runtime), gateway(runtime)
            one = asyncio.create_task(first.execute("notes_search", {"text": "会议纪要"}, "one"))
            await started.wait()
            two = asyncio.create_task(second.execute("notes_search", {"text": "会议纪要"}, "two"))
            await asyncio.sleep(0.02)
            runtime.store.cancel(runtime.principals["reader"], first.task_id)
            one.cancel()
            with pytest.raises(asyncio.CancelledError):
                await one
            finish.set()
            assert (await two)["evidence"] and calls == [1]
        asyncio.run(run())


def test_freshness_identity_and_revocation_are_not_bypassed(tmp_path):
    app = application(tmp_path)
    with TestClient(app, base_url="http://localhost"):
        runtime = app.state.runtime
        original = runtime.catalog._handlers[("notes", "notes_search")]
        calls = []
        def handler(arguments, retrieve):
            calls.append(1)
            return original(arguments, retrieve)
        runtime.catalog._handlers[("notes", "notes_search")] = handler
        async def run():
            for _ in range(2):
                await gateway(runtime).execute("notes_search", {"text": "会议纪要"}, "same")
            assert len(calls) == 1
            runtime.read_cache.policies["notes/notes_search"]["freshness"] = "v2"
            await gateway(runtime).execute("notes_search", {"text": "会议纪要"}, "new")
            assert len(calls) == 2
            other = {**runtime.principals["reader"], "user_id": "other"}
            runtime.principals["other"] = other
            await gateway(runtime, other).execute("notes_search", {"text": "会议纪要"}, "other")
            assert len(calls) == 3
            pending = gateway(runtime)
            runtime.releases.revoke_capability("notes", "notes_search", actor=runtime.principals["reader"])
            with pytest.raises(Fault) as denied:
                await pending.execute("notes_search", {"text": "会议纪要"}, "revoked")
            assert denied.value.code == "CAPABILITY_REVOKED" and len(calls) == 3
        asyncio.run(run())
