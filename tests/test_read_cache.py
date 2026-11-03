"""共享只读结果不能共享授权、取消或可变对象。"""
import asyncio

import pytest

from app.errors import Fault
from app.read_cache import ReadCache


def participant(value=1):
    return {"guard": lambda: None, "charge": lambda: None, "value": value}


def cache():
    return ReadCache({"tools": {"notes/notes_search": {"ttl_s": 1, "freshness": "revision-1"}}})


def test_single_flight_cancellation_isolation_and_copy():
    async def run():
        pool = cache()
        started, finish = asyncio.Event(), asyncio.Event()
        calls = []
        async def operation(select):
            calls.append(select(True))
            started.set()
            await finish.wait()
            return {"data": [select()]}
        first = asyncio.create_task(pool.run("same", 1, participant(1), operation))
        await started.wait()
        second = asyncio.create_task(pool.run("same", 1, participant(2), operation))
        await asyncio.sleep(0.02)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        finish.set()
        result = await second
        assert result == {"data": [2]} and calls == [1]
        result["data"].append(99)
        assert await pool.run("same", 1, participant(), operation) == {"data": [2]}
        assert not pool.flights
    asyncio.run(run())


def test_all_waiters_cancel_reclaims_physical_operation():
    async def run():
        pool = cache()
        started, stopped = asyncio.Event(), asyncio.Event()
        async def operation(select):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        task = asyncio.create_task(pool.run("same", 1, participant(), operation))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stopped.is_set() and not pool.flights and not pool.entries
    asyncio.run(run())


def test_revoked_waiter_and_budget_failure_do_not_poison_survivor():
    async def run():
        pool = cache()
        ready, proceed = asyncio.Event(), asyncio.Event()
        expired = participant(1)
        def deny():
            raise Fault("BUDGET_EXCEEDED", "测试预算已用尽", 422)
        async def operation(select):
            ready.set()
            await proceed.wait()
            return {"owner": select(True)}
        one = asyncio.create_task(pool.run("same", 1, expired, operation))
        await ready.wait()
        two = asyncio.create_task(pool.run("same", 1, participant(2), operation))
        await asyncio.sleep(0.02)
        expired["charge"] = deny
        proceed.set()
        with pytest.raises(Fault, match="测试预算"):
            await one
        assert await two == {"owner": 2}
        rejected = participant()
        rejected["guard"] = deny
        with pytest.raises(Fault):
            await pool.run("same", 1, rejected, operation)
    asyncio.run(run())


def test_ttl_failures_and_capacity_are_bounded():
    async def run():
        pool = ReadCache({"max_entries": 1, "tools": {}})
        calls = []
        async def operation(select):
            calls.append(select())
            return {"n": len(calls)}
        assert (await pool.run("a", 0.01, participant(), operation))["n"] == 1
        await asyncio.sleep(0.02)
        assert (await pool.run("a", 1, participant(), operation))["n"] == 2
        await pool.run("b", 1, participant(), operation)
        assert len(pool.entries) == 1
        async def fail(select):
            raise Fault("UPSTREAM_ERROR", "测试失败", 502)
        for _ in range(2):
            with pytest.raises(Fault):
                await pool.run("bad", 1, participant(), fail)
        assert "bad" not in pool.entries and not pool.flights
    asyncio.run(run())


@pytest.mark.parametrize("settings", [{"tools": {"a": {"ttl_s": 1}}}, {"tools": {"a": {"ttl_s": 0, "freshness": "x"}}}, {"max_entries": True, "tools": {}}])
def test_invalid_policy_fails_closed(settings):
    with pytest.raises(ValueError):
        ReadCache(settings)
