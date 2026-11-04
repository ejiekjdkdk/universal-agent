"""检索能力的紧急停用也必须作用于实际检索入口。"""
import asyncio

import pytest

from app.errors import Fault
from app.main import create_app


@pytest.mark.parametrize("through_adapter", [False, True])
def test_revoked_retrieval_is_rejected_before_reading_documents(tmp_path, through_adapter):
    app = create_app(database=tmp_path / "agent.db", token="reader", model_settings={})

    async def run():
        async with app.router.lifespan_context(app):
            runtime = app.state.runtime
            actor = runtime.principals["reader"]
            task, _ = runtime.store.start(actor, {"business_id": "notes", "mode": "mock", "message": "会议纪要"}, None, "1")
            gateway = runtime.gateway(runtime.store.context_id(task["task_id"]))
            assert await gateway.retrieve("会议纪要")
            runtime.releases.revoke_capability("notes", "retrieve_knowledge", actor=actor)
            with pytest.raises(Fault) as rejected:
                if through_adapter:
                    await gateway.execute("notes_search", {"text": "会议纪要"}, "after-revocation")
                else:
                    await gateway.retrieve("会议纪要")
            assert rejected.value.code == "CAPABILITY_REVOKED"

    asyncio.run(run())


@pytest.mark.parametrize("already_used_cache", [False, True])
def test_shared_retrieval_result_retains_dependency_for_each_task(tmp_path, already_used_cache):
    settings = {"p2": {"read_cache": {"tools": {"notes/notes_search": {"ttl_s": 60, "freshness": "v1"}}}}}
    app = create_app(database=tmp_path / "agent.db", token="reader", model_settings=settings)

    async def run():
        async with app.router.lifespan_context(app):
            runtime = app.state.runtime
            actor = runtime.principals["reader"]
            gateways = []
            for _ in range(2):
                task, _ = runtime.store.start(actor, {"business_id": "notes", "mode": "mock", "message": "会议纪要"}, None, "1")
                gateways.append(runtime.gateway(runtime.store.context_id(task["task_id"])))
            assert (await gateways[0].execute("notes_search", {"text": "会议纪要"}, "read"))["evidence"]
            if already_used_cache:
                assert (await gateways[1].execute("notes_search", {"text": "会议纪要"}, "read"))["evidence"]
            runtime.releases.revoke_capability("notes", "retrieve_knowledge", actor=actor)
            with pytest.raises(Fault) as rejected:
                await gateways[1].execute("notes_search", {"text": "会议纪要"}, "read")
            assert rejected.value.code == "CAPABILITY_REVOKED"

    asyncio.run(run())
