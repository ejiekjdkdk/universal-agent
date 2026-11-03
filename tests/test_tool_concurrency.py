import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import threading

import pytest

from app.catalog import Catalog
from app.errors import Fault
from app.runtime import Gateway, Runtime
from app.store import Store


@pytest.mark.parametrize("stop", ["cancel", "timeout"])
def test_sync_threads_hold_gateway_capacity_until_they_exit(tmp_path, stop):
    catalog = Catalog(Path(__file__).resolve().parents[1] / "scenarios")
    actor = {"tenant_id": "t", "org_id": "o", "user_id": "u",
             "grants": {"text": {"permissions": ["text.read"]}}}
    tool_key = ("text", "text_stats")
    original = catalog._handlers[tool_key]
    release = threading.Event()

    async def exercise(runtime):
        loop = asyncio.get_running_loop()
        # 默认线程池在小型机器上可能不足九个线程，无法揭示网关泄漏。
        loop.set_default_executor(ThreadPoolExecutor(max_workers=10))
        entered = [asyncio.Event() for _ in range(9)]

        def blocking(arguments, retrieve):
            index = int(arguments["text"])
            loop.call_soon_threadsafe(entered[index].set)
            if index < 8:
                release.wait(10)
            return original(arguments, retrieve)

        catalog._handlers[tool_key] = blocking
        calls = []
        for index in range(9):
            task, _ = runtime.store.start(actor, {"business_id": "text", "message": "run"}, None, "1")
            gateway = Gateway(runtime, runtime.store.context_id(task["task_id"]))
            calls.append(gateway)
        if stop == "timeout":
            runtime.tool_timeout = 0.5
        active = [asyncio.create_task(calls[i].execute("text_stats", {"text": str(i)}, "call"))
                  for i in range(8)]
        try:
            await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered[:8])), 3)
            if stop == "cancel":
                for task in active:
                    task.cancel()
            results = await asyncio.wait_for(asyncio.gather(*active, return_exceptions=True), 2)
            if stop == "cancel":
                assert all(isinstance(result, asyncio.CancelledError) for result in results)
            else:
                assert all(isinstance(result, Fault) and result.code == "TOOL_TIMEOUT" for result in results)

            runtime.tool_timeout = 0.1
            with pytest.raises(Fault, match="工具超时"):
                await calls[8].execute("text_stats", {"text": "8"}, "queued")
            assert not entered[8].is_set()
        finally:
            release.set()
            await asyncio.gather(*active, return_exceptions=True)

        runtime.tool_timeout = 2
        result = await calls[8].execute("text_stats", {"text": "8"}, "after-release")
        assert result["status"] == "completed"

    with closing(Store(tmp_path / "tasks.db")) as store:
        asyncio.run(exercise(Runtime(catalog, store, {"owner": actor}, {})))
