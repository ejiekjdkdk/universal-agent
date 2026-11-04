"""真实双 Worker 与本地虚构账本的审批、崩溃和结果未知验收。"""
from contextlib import ExitStack, closing
import json
from pathlib import Path
import shutil
import sqlite3
from types import SimpleNamespace

import pytest

from app.store import Store
from p4_helpers import client, environment, launch, start_api
from test_worker_recovery import kill, wait_until


ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / "examples" / "governance"
API = "/api/v1/agent"
TERMINAL = {"task_completed", "task_failed", "task_cancelled"}

# 只在测试子进程包裹原类的方法。事务体、COMMIT、领取和审批实现均来自产品。
# before_commit 屏障仍持有原事务；after_commit 屏障位于原上下文退出之后。
WORKER = r'''
import asyncio
from contextlib import contextmanager
import json
import os
from pathlib import Path
import time

from app import approvals
from app.store import Store
from app.worker import main

probe = Path(os.environ["P4_WRITE_PROBE"])
mode = os.environ["P4_WRITE_MODE"]
pid = os.getpid()
original_transaction = Store._transaction
original_process = Store.process_mock_write
original_claim = Store.claim_step
original_verify = approvals.verify_receipt
polls = 0


def pause(claim):
    (probe / "barrier.json").write_text(json.dumps({"pid": pid, "claim": claim}), encoding="utf-8")
    deadline = time.monotonic() + 60
    while not (probe / "release").exists():
        if time.monotonic() > deadline:
            raise TimeoutError("测试没有释放事务屏障")
        time.sleep(.03)


@contextmanager
def transaction(self, *args, **kwargs):
    claim = getattr(self, "_p4_selected_claim", None)
    outer = claim is not None and not self._db.in_transaction
    with original_transaction(self, *args, **kwargs):
        yield
        if outer and mode == "before_commit":
            pause(claim)
    if outer and mode in {"normal", "after_commit", "lost_ack"}:
        pause(claim)


def process(self, runtime, claim, tool, args):
    selected = False
    if claim["phase"] == "execution":
        name = f"attempt-{pid}-{claim['task_id']}-{claim['step_id']}-{claim['token']}.json"
        (probe / name).write_text(json.dumps(claim), encoding="utf-8")
        try:
            with (probe / "selected").open("x", encoding="utf-8") as target:
                target.write(str(pid))
            selected = True
        except FileExistsError:
            pass
    self._p4_selected_claim = claim if selected else None
    try:
        return original_process(self, runtime, claim, tool, args)
    finally:
        self._p4_selected_claim = None


def claim(self, *args, **kwargs):
    global polls
    result = original_claim(self, *args, **kwargs)
    polls += 1
    (probe / f"poll-{pid}-{polls:06d}").touch()
    return result


def lost_ack(db, receipt_id, digest, version):
    # 原校验先确认事务内真实回执，再模拟确认传输丢失。
    original_verify(db, receipt_id, digest, version)
    (probe / f"lost-ack-{pid}").write_text(receipt_id, encoding="utf-8")
    raise TimeoutError("虚构确认丢失")


Store._transaction = transaction
Store.process_mock_write = process
Store.claim_step = claim
if mode == "lost_ack":
    approvals.verify_receipt = lost_ack
asyncio.run(main())
'''


@pytest.fixture
def system(tmp_path):
    """同一真实 API、同一 SQLite，以及两个直接启动的 Worker PID。"""
    scenes = tmp_path / "scenarios"
    shutil.copytree(PROJECT / "scenarios", scenes, ignore=shutil.ignore_patterns("__pycache__"))
    roles = tmp_path / "principals.json"
    shutil.copyfile(PROJECT / "principals.json", roles)
    probe = tmp_path / "probe"
    probe.mkdir()
    env = environment(tmp_path, scenes)
    env.update(AGENT_PRINCIPALS_FILE=str(roles), AGENT_APPROVER_TOKEN="p4-fictional-reviewer",
               AGENT_MOCK_WRITES="1", AGENT_PLAN_WORKER="external", AGENT_PLAN_LEASE_SECONDS="2",
               P4_WRITE_PROBE=str(probe))
    script = tmp_path / "worker.py"
    script.write_text(WORKER, encoding="utf-8")
    with ExitStack() as stack:
        connection = client(stack, env)
        api = start_api(stack, ROOT, env, tmp_path, "api", connection)
        s = SimpleNamespace(path=tmp_path, scenes=scenes, probe=probe, env=env, connection=connection,
                            stack=stack, api=api, workers=[], script=script, db=Path(env["AGENT_DATABASE"]),
                            reviewer={"Authorization": "Bearer p4-fictional-reviewer"})
        yield s


def workers(s, mode):
    for number in range(2):
        child_env = {**s.env, "P4_WRITE_MODE": mode}
        s.workers.append(launch(s.stack, ROOT, child_env, s.path, f"worker-{number}", str(s.script)))
    assert s.workers[0][0].pid != s.workers[1][0].pid
    wait_until(lambda: all(list(s.probe.glob(f"poll-{p.pid}-*")) for p, _ in s.workers),
               [s.api, *s.workers], "两个真实 Worker 已轮询同一个数据库")


def submit(s):
    response = s.connection.post(API + "/chat", json={"business_id": "change_requests", "mode": "offline",
        "execution_mode": "durable_plan", "message": "纯虚构修订内容", "context": {"object_ids": ["draft-1"]}})
    assert response.status_code == 202, response.text
    return response.json()["task_id"]


def task(s, tid):
    return s.connection.get(f"{API}/tasks/{tid}").raise_for_status().json()


def pending(s, tid, step="publish"):
    def find():
        response = s.connection.get(API + "/approvals?status=requested", headers=s.reviewer)
        assert response.status_code == 200, response.text
        return next((row for row in response.json()["approvals"]
                     if row["task_id"] == tid and row["step_id"] == step), None)
    result = wait_until(find, [s.api, *s.workers], "等待真实审批提案")
    assert task(s, tid)["status"] == "waiting_approval"
    return result


def approve(s, proposal):
    response = s.connection.post(f"{API}/approvals/{proposal['approval_id']}/decision",
        headers=s.reviewer | {"Idempotency-Key": "p4-approve-" + proposal["approval_id"]},
        json={"decision": "approve", "proposal_digest": proposal["proposal_digest"]})
    assert response.status_code == 200, response.text


def barrier(s):
    def read():
        path = s.probe / "barrier.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return None
    data = wait_until(read, [s.api, *s.workers], "真实事务故障屏障")
    assert data["pid"] in {p.pid for p, _ in s.workers}
    selected = next(item for item in s.workers if item[0].pid == data["pid"])
    other = next(item for item in s.workers if item[0].pid != data["pid"])
    assert selected[0].poll() is other[0].poll() is None
    return data, selected, other


def observe_polls(s, processes):
    """至少观察后续三次真实领取扫描，避免用固定睡眠推断未重发。"""
    before = {p.pid: len(list(s.probe.glob(f"poll-{p.pid}-*"))) for p, _ in processes}
    wait_until(lambda: all(len(list(s.probe.glob(f"poll-{p.pid}-*"))) >= before[p.pid] + 3
                           for p, _ in processes), [s.api, *processes], "接手 Worker 继续扫描且未重发")


def evidence(s, tid):
    with closing(sqlite3.connect(s.db)) as db:
        return {"ledger": db.execute("SELECT object_id,version,value FROM mock_ledger").fetchall(),
                "receipts": db.execute("SELECT receipt_id,object_version FROM mock_receipts").fetchall(),
                "calls": db.execute("SELECT execution_id,status,error FROM calls WHERE task_id=?", (tid,)).fetchall(),
                "steps": db.execute("SELECT step_id,status,token FROM plan_steps WHERE task_id=?", (tid,)).fetchall(),
                "events": [(name, json.loads(payload)) for name, payload in db.execute(
                    "SELECT event,payload FROM events WHERE task_id=? ORDER BY event_id", (tid,))]}


def unique_terminal(data, expected):
    terminal = [(name, payload) for name, payload in data["events"] if name in TERMINAL]
    assert len(terminal) == 1 and terminal[0][0] == expected, terminal
    return terminal[0][1]


def single_write(data, call_status="completed"):
    assert data["ledger"] == [("draft-1", 1, "纯虚构修订内容")]
    assert len(data["receipts"]) == len(data["calls"]) == 1
    assert data["receipts"][0][1] == 1
    assert data["calls"][0][1] == call_status


@pytest.mark.parametrize("fault", ["normal", "before_commit", "after_commit"])
def test_two_workers_commit_once(system, fault):
    s = system
    workers(s, fault)
    tid = submit(s)
    proposal = pending(s, tid)
    approve(s, proposal)
    captured, selected, other = barrier(s)
    before = evidence(s, tid)
    if fault == "before_commit":
        # 独立连接看不到未 COMMIT 的账本、回执、调用或成功终态。
        assert before["ledger"] == before["receipts"] == before["calls"] == []
        assert not [name for name, _ in before["events"] if name in TERMINAL]
    else:
        single_write(before)
        unique_terminal(before, "task_completed")
    if fault == "normal":
        (s.probe / "release").touch()
        active = s.workers
    else:
        kill(selected[0])
        assert selected[0].returncode is not None
        active = [other]
    wait_until(lambda: task(s, tid)["status"] == "completed", [s.api, *active], "事务恢复后完成")
    observe_polls(s, active)
    final = evidence(s, tid)
    single_write(final)
    unique_terminal(final, "task_completed")
    attempts = [json.loads(path.read_text(encoding="utf-8")) for path in s.probe.glob("attempt-*")]
    assert len(attempts) == (2 if fault == "before_commit" else 1)
    if fault == "before_commit":
        assert final["steps"][0][2] > captured["claim"]["token"]
        assert list(s.probe.glob(f"attempt-{other[0].pid}-*")), "必须由另一个真实 PID 接手执行"
    else:
        assert final["steps"][0][2] == captured["claim"]["token"]
    # 将捕获的原执行身份交回原 Store；旧身份不能再结算、续租或覆盖成功。
    with closing(Store(s.db, recover_reactive=False)) as store:
        assert not store.renew_step(captured["claim"], 2)
        assert not store.settle_step(captured["claim"], result={"data": {"summary": "伪造旧身份成功"}, "evidence": []})
    assert evidence(s, tid) == final


def test_two_workers_do_not_resend_unknown_commit(system):
    s = system
    workers(s, "lost_ack")
    tid = submit(s)
    proposal = pending(s, tid)
    approve(s, proposal)
    _, selected, other = barrier(s)
    assert list(s.probe.glob(f"lost-ack-{selected[0].pid}"))
    unknown = task(s, tid)
    assert unknown["status"] == "failed" and unknown["error"]["code"] == "OUTCOME_UNKNOWN"
    initial = evidence(s, tid)
    single_write(initial, "unknown_outcome")
    terminal = unique_terminal(initial, "task_failed")
    assert terminal["error"]["code"] == "OUTCOME_UNKNOWN"
    assert json.loads(initial["calls"][0][2])["code"] == "OUTCOME_UNKNOWN"
    kill(selected[0])
    observe_polls(s, [other])
    assert evidence(s, tid) == initial
    assert len(list(s.probe.glob("attempt-*"))) == 1
    assert task(s, tid)["error"]["code"] == "OUTCOME_UNKNOWN"
    path = f"{API}/approvals/{proposal['approval_id']}"
    detail = s.connection.get(path, headers=s.reviewer).raise_for_status().json()
    assert detail["commit_status"] == "outcome_unknown"
    assert detail["receipt_id"] == initial["receipts"][0][0]
    for headers in ({}, s.reviewer):
        response = s.connection.get(path + "/receipt", headers=headers)
        assert response.status_code == 200, response.text
        receipt = response.json()
        assert receipt["commit_status"] == "outcome_unknown" and receipt["verification_status"] == "confirmed"
        assert receipt["receipt_id"] == detail["receipt_id"]
        assert receipt["object_version"] == receipt["current_version"] == 1
        assert "纯虚构修订内容" not in response.text
    assert terminal["mock_commits"][0]["receipt_id"] == detail["receipt_id"]
    assert evidence(s, tid) == initial


@pytest.mark.parametrize("already_committed", [False, True])
def test_two_workers_respect_cancelled_approval(system, already_committed):
    s = system
    if already_committed:
        path = s.scenes / "change_requests" / "plan.json"
        plan = json.loads(path.read_text(encoding="utf-8"))
        plan["steps"].append({**plan["steps"][0], "step_id": "followup", "depends_on": ["publish"]})
        path.write_text(json.dumps(plan), encoding="utf-8")
        # API 已启动但尚未建任务，原目录通过重启重新装载，仍使用同一状态库。
        kill(s.api[0])
        s.api = start_api(s.stack, ROOT, s.env, s.path, "api-two-steps", s.connection)
    workers(s, "observe")
    tid = submit(s)
    proposal = pending(s, tid)
    if already_committed:
        approve(s, proposal)
        proposal = pending(s, tid, "followup")
    initial = evidence(s, tid)
    if already_committed:
        single_write(initial)
    else:
        assert initial["ledger"] == initial["receipts"] == initial["calls"] == []
    response = s.connection.post(f"{API}/tasks/{tid}/cancel")
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "cancelled"
    observe_polls(s, s.workers)
    final = evidence(s, tid)
    assert final["ledger"] == initial["ledger"]
    assert final["receipts"] == initial["receipts"]
    assert final["calls"] == initial["calls"]
    assert not [status for _, status, _ in final["steps"] if status in {"leased", "running", "ready", "waiting_approval"}]
    terminal = unique_terminal(final, "task_cancelled")
    if already_committed:
        assert terminal["mock_commits"][0]["receipt_id"] == initial["receipts"][0][0]
        assert terminal["mock_commits"][0]["object_version"] == 1
    else:
        assert not list(s.probe.glob("attempt-*"))
        assert not terminal.get("mock_commits")
    refused = s.connection.post(f"{API}/approvals/{proposal['approval_id']}/decision",
        headers=s.reviewer | {"Idempotency-Key": "late-approval"},
        json={"decision": "approve", "proposal_digest": proposal["proposal_digest"]})
    assert refused.status_code == 409, refused.text
    observe_polls(s, s.workers)
    assert evidence(s, tid) == final
