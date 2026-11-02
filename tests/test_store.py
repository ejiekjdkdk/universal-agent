import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from app.errors import Fault
from app.store import Store


OWNER = {"tenant_id": "t1", "org_id": "g1", "user_id": "u1"}
REQUEST = {"business_id": "general", "message": "hello"}


class StoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "state.sqlite"
        self.store = Store(self.path)
        self.addCleanup(lambda: self.store.close())

    def start(self, **changes):
        return self.store.start(OWNER, {**REQUEST, **changes}, None, "1")[0]

    def test_key_reuses_task_and_anonymous_session_even_under_contention(self):
        def submit(_):
            return self.store.start(OWNER, REQUEST, "same", "1")
        with ThreadPoolExecutor(max_workers=4) as pool:
            submissions = list(pool.map(submit, range(12)))
        self.assertEqual(sum(created for _, created in submissions), 1)
        self.assertEqual(len({t["session_id"] for t, _ in submissions}), 1)
        self.assertEqual(len(self.store.recent(OWNER)), 1)
        task, created = self.store.start(OWNER, dict(reversed(list(REQUEST.items()))), "same", "1")
        self.assertFalse(created)
        self.assertEqual(task["task_id"], submissions[0][0]["task_id"])
        with self.assertRaises(Fault) as conflict:
            self.store.start(OWNER, {**REQUEST, "message": "different"}, "same", "1")
        self.assertEqual(conflict.exception.code, "IDEMPOTENCY_CONFLICT")

    def test_resources_and_idempotency_are_scoped_by_each_identity_field(self):
        original, _ = self.store.start(OWNER, REQUEST, "key", "1")
        for dimension in OWNER:
            other = {**OWNER, dimension: "other"}
            self.assertEqual(self.store.recent(other), [])
            for method, identifier in [(self.store.session, "session_id"),
                                       (self.store.history, "session_id"),
                                       (self.store.task, "task_id"),
                                       (self.store.cancel, "task_id")]:
                with self.subTest(dimension=dimension, method=method.__name__), self.assertRaises(Fault):
                    method(other, original[identifier])
            copy, created = self.store.start(other, REQUEST, "key", "1")
            self.assertTrue(created)
            self.assertNotEqual(copy["task_id"], original["task_id"])
        with self.assertRaises(Fault):
            self.start(session_id=original["session_id"], business_id="another")

    def test_server_context_is_snapshotted_scoped_and_revoked_on_finish(self):
        context = {"object_type": "document", "object_ids": ["a", "b"]}
        session = self.store.create_session(OWNER, "general", context)
        sid = session["session_id"]
        inherited = self.start(session_id=sid)
        run_id = self.store.context_id(inherited["task_id"])
        run = self.store.run(run_id)
        self.assertEqual(run["object_ids"], ["a", "b"])
        self.assertEqual(run["object_type"], "document")
        self.assertEqual(run["org_id"], "g1")
        self.assertNotIn("run_context_id", inherited)
        narrow = self.start(session_id=sid, context={"object_ids": ["b"]})
        self.assertEqual(self.store.run(self.store.context_id(narrow["task_id"]))["object_ids"], ["b"])
        with self.assertRaises(Fault):
            self.start(session_id=sid, context={"object_ids": ["x"]})
        self.assertEqual(self.store.session(OWNER, sid)["context"], context)
        self.store.cancel(OWNER, inherited["task_id"])
        self.assertEqual(self.store.scope(inherited["task_id"]), context)
        with self.assertRaises(Fault):
            self.store.run(run_id)

    def test_terminal_write_is_atomic_and_replay_resumes_after_cursor(self):
        task = self.start()
        tid = task["task_id"]
        self.store.emit(tid, "message_delta", {"delta": "好"})
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.store.finish(tid, "partial", {"answer": "好"}), range(2)))
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(self.store.cancel(OWNER, tid)["status"], "partial")
        self.store.emit(tid, "message_delta", {"delta": "late"})
        events = self.store.events(tid, 1)
        self.assertEqual([e["event"] for e in events], ["message_delta", "task_completed"])
        self.assertEqual([e["id"] for e in events], [2, 3])
        self.assertTrue(all(e["timestamp"] and e["data"]["task_id"] == tid for e in events))
        self.assertEqual(events[-1]["data"]["result"]["answer"], "好")

    def test_tool_call_replay_preserves_execution_and_charges_budget_once(self):
        tid = self.start()["task_id"]
        arguments = {"input": "PRIVATE_ARGUMENT_VALUE"}
        execution, result = self.store.begin_call(tid, "c", "lookup", "1", arguments, 1)
        self.assertIsNone(result)
        with self.assertRaises(Fault):
            self.store.begin_call(tid, "c", "lookup", "1", arguments, 1)
        answer = {"execution_id": execution, "status": "completed", "data": {"x": 1}}
        self.store.complete_call(tid, "c", answer)
        self.assertEqual(self.store.begin_call(tid, "c", "lookup", "1", arguments, 1), (execution, answer))
        for call_id, args in [("c", {}), ("new", arguments)]:
            with self.assertRaises(Fault):
                self.store.begin_call(tid, call_id, "lookup", "1", args, 1)
        self.assertEqual(self.store.task(OWNER, tid)["tool_calls"][0]["status"], "completed")
        self.assertGreaterEqual(self.store.task(OWNER, tid)["tool_calls"][0]["duration_ms"], 0)
        self.assertEqual([e["event"] for e in self.store.events(tid, 0)],
                         ["task_started", "step_started", "step_completed"])
        with closing(sqlite3.connect(self.path)) as db:
            self.assertNotIn("PRIVATE_ARGUMENT_VALUE", "\n".join(db.iterdump()))

    def test_failed_tool_is_not_reexecuted(self):
        tid = self.start()["task_id"]
        self.store.begin_call(tid, "c", "lookup", "1", {})
        error = {"code": "TOOL_TIMEOUT", "message": "timeout", "retryable": False}
        self.store.complete_call(tid, "c", {}, error)
        with self.assertRaises(Fault) as failure:
            self.store.begin_call(tid, "c", "lookup", "1", {})
        self.assertEqual(failure.exception.code, "TOOL_TIMEOUT")
        self.assertEqual(self.store.events(tid, 1)[-1]["event"], "step_failed")

    def test_finished_task_retains_call_summary_and_discards_replay_only_payload(self):
        tid = self.start()["task_id"]
        self.store.begin_call(tid, "lookup", "lookup", "1", {})
        self.store.complete_call(tid, "lookup", {"data": "TEMPORARY_TOOL_PAYLOAD"})
        self.store.finish(tid, "completed", {"answer": "summary"})
        self.assertEqual(self.store.task(OWNER, tid)["tool_calls"][0]["capability_id"], "lookup")
        with closing(sqlite3.connect(self.path)) as db:
            self.assertNotIn("TEMPORARY_TOOL_PAYLOAD", "\n".join(db.iterdump()))

    def test_expired_context_cannot_open_a_new_tool_execution(self):
        with closing(Store(":memory:", run_ttl_seconds=0)) as store:
            task, _ = store.start(OWNER, REQUEST, None, "1")
            tid = task["task_id"]
            with self.assertRaises(Fault):
                store.run(store.context_id(tid))
            with self.assertRaises(Fault):
                store.begin_call(tid, "c", "lookup", "1", {})
            self.assertEqual(store.task(OWNER, tid)["tool_calls"], [])

    def test_restart_fails_incomplete_work_only_once_and_preserves_audits(self):
        task = self.start()
        tid = task["task_id"]
        self.store.begin_call(tid, "c", "lookup", "1", {})
        audit_id = self.store.audit("policy", "deny", OWNER, tid, "lookup", "PERMISSION_DENIED")
        for _ in range(2):
            self.store.close()
            self.store = Store(self.path)
        recovered = self.store.task(OWNER, tid)
        self.assertEqual(recovered["status"], "failed")
        self.assertEqual(recovered["tool_calls"][0]["status"], "unknown_outcome")
        self.assertEqual(sum(e["event"] == "task_failed" for e in self.store.events(tid, 0)), 1)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertIn(audit_id, "\n".join(db.iterdump()))

    def test_history_uses_only_recent_completed_turns_and_recent_filters_by_scenario(self):
        first = self.start(message="one", mode="online")
        self.store.finish(first["task_id"], "completed", {"answer": "first answer"})
        sid = first["session_id"]
        later = self.start(session_id=sid, message="two")
        self.store.finish(later["task_id"], "completed", {"answer": "second answer"})
        self.store.cancel(OWNER, self.start(session_id=sid, message="cancel me")["task_id"])
        self.start(business_id="other")
        self.assertEqual(self.store.history(OWNER, sid, 1),
                         [{"role": "user", "content": "two"}, {"role": "assistant", "content": "second answer"}])
        self.assertEqual(len(self.store.history(OWNER, sid)), 4)
        recent = self.store.recent(OWNER, "general", 3)
        self.assertEqual([t["status"] for t in recent], ["cancelled", "completed", "completed"])
        self.assertEqual(recent[-1]["mode"], "online")
        self.assertEqual(recent[0]["mode"], "offline")

    def test_history_filters_granted_objects_and_types_before_limiting_turns(self):
        session = self.store.create_session(OWNER, "general", {})
        for object_type, obj in (("document", "public"), ("document", "private"), ("other", "public")):
            task = self.start(message=f"read {object_type}/{obj}", session_id=session["session_id"],
                              context={"object_type": object_type, "object_ids": [obj]})
            self.store.finish(task["task_id"], "completed", {"answer": f"content of {object_type}/{obj}"})
        visible = self.store.history(OWNER, session["session_id"], limit=1,
                                     allowed_objects=["public"], allowed_types=["document"])
        self.assertEqual(visible, [{"role": "user", "content": "read document/public"},
                                   {"role": "assistant", "content": "content of document/public"}])
