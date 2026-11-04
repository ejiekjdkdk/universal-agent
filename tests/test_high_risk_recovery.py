"""双连接接管时，已封存结果必须恢复或明确失败，旧租约不能覆盖。"""
from contextlib import ExitStack, closing
import time

import pytest

from app.errors import Fault
from app.result_store import ResultStore
from app.store import Store


@pytest.mark.parametrize("deleted", [False, True])
def test_cross_connection_encrypted_call_recovery_fences_stale_worker(tmp_path, monkeypatch, deleted):
    monkeypatch.setenv("HIGH_RISK_RECOVERY_KEY", "92" * 32)
    now = [time.time()]
    monkeypatch.setattr(time, "time", lambda: now[0])
    actor = {"tenant_id": "t", "org_id": "o", "user_id": "u"}
    policy = {"keys": {"test": "HIGH_RISK_RECOVERY_KEY"}, "active_key": "test"}
    with ExitStack() as stack:
        first, second = [stack.enter_context(closing(Store(tmp_path / "db", recover_reactive=False)))
                         for _ in range(2)]
        for store in (first, second):
            store._result_store = ResultStore(store, policy)
        plan = {"schema_version": 1, "result_contract": "summary_v1", "steps": [
            {"step_id": "read", "capability_id": "read", "arguments": {}, "depends_on": [], "required": True}]}
        budget = {"max_parallel_steps": 1, "max_steps": 2, "max_tool_calls": 2, "deadline_ms": 60000}
        task, _ = first.start(actor, {"business_id": "notes", "message": "查询"}, None, "1",
                              plan=plan, snapshot_id="snapshot", budget=budget)
        task_id = task["task_id"]
        old = first.claim_step("first", 1)
        call_args = (task_id, "plan.read", "read", "1", {})
        execution_id, _ = first.begin_call(*call_args, lease=old)
        result = {"execution_id": execution_id, "data": {"summary": "已确认结果"}, "evidence": []}
        with first._transaction():
            ref = first._result_store.put(task_id, result, {"snapshot_id": "snapshot"})
            first.complete_call(task_id, "plan.read", result, lease=old)
        if deleted:
            second._result_store.revoke(ref, actor, lambda *args: None)
        now[0] += 2
        current = second.claim_step("second", 10)
        assert current["token"] > old["token"]
        assert not first.settle_step(old, result=result)
        with pytest.raises(Fault, match="lease"):
            first.complete_call(task_id, "plan.read", result, lease=old)
        if deleted:
            with pytest.raises(Fault) as missing:
                second.begin_call(*call_args, lease=current)
            assert missing.value.code == "RESULT_NOT_FOUND"
            assert second.settle_step(current, error=missing.value.as_dict())
            assert first.task(actor, task_id)["status"] == "failed"
        else:
            recovered_id, recovered = second.begin_call(*call_args, lease=current)
            assert recovered_id == execution_id and recovered == result
            assert second.settle_step(current, result=recovered)
            assert first.task(actor, task_id)["status"] == "completed"
        assert first._db.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 1
        assert first._db.execute("SELECT COUNT(*) FROM plan_steps WHERE output IS NOT NULL").fetchone()[0] == 0
