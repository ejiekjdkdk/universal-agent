"""真实网络断线与候选/基线程序顺序切换的本机发布验收。"""
from contextlib import ExitStack
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tarfile
import time

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.skills import SkillRegistry
from p4_helpers import backup_state, client, environment, frames, start_api
from test_worker_recovery import ADAPTER, kill, wait_until


ROOT = Path(__file__).resolve().parents[1]
API = "/api/v1/agent"
ADMIN = "/api/v1/agent-admin"
BASELINE = "ed4f85bac11826e2d1bc0814f39a6b1405e6f093"
PUBLISHER = {"Authorization": "Bearer p4-fictional-admin"}
BODY = {"business_id": "notes", "message": "会议纪要", "mode": "offline"}
TERMINALS = {"task_completed", "task_failed", "task_cancelled", "task_partial"}

# 只在测试启动程序包装真正的同步适配器边界；发布包和固定金标摘要保持原样。
COUNTED_API = '''
import os
from pathlib import Path
from uuid import uuid4
import app.catalog as catalog
import uvicorn
original_call = catalog._call_sync
async def counted_call(*args, **kwargs):
    (Path(os.environ["P4_RELEASE_PROBE"]) / uuid4().hex).touch()
    return await original_call(*args, **kwargs)
catalog._call_sync = counted_call
uvicorn.run("app.main:create_app", factory=True, host="127.0.0.1",
            port=int(os.environ["AGENT_PORT"]), proxy_headers=False)
'''


def copy_scene(source, target):
    return shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))


def finished(connection, path):
    task = connection.get(path).raise_for_status().json()
    if task["status"] in {"failed", "partial", "cancelled"}:
        raise AssertionError(task)
    return task if task["status"] == "completed" else None


def replay(connection, path, cursor=0):
    with connection.stream("GET", path + "/events", headers={"Last-Event-ID": str(cursor)}) as response:
        response.raise_for_status()
        return list(frames(response.iter_lines()))


@pytest.mark.parametrize("execution_mode", ["reactive", "durable_plan"])
def test_real_disconnect_replays_only_persisted_events(tmp_path, execution_mode):
    scenes = tmp_path / "scenarios"
    probe = tmp_path / "probe"
    probe.mkdir()
    durable = execution_mode == "durable_plan"
    scene_id = "project_brief" if durable else "notes"
    source = ROOT / "examples/durable/scenarios" if durable else ROOT / "scenarios"
    scene = copy_scene(source / scene_id, scenes / scene_id)
    if durable:
        (scene / "adapter.py").write_text(ADAPTER.format(release="sse"), encoding="utf-8")
    else:
        adapter = scene / "adapter.py"
        adapter.write_text(adapter.read_text(encoding="utf-8") + '''

_original_search = search
def search(arguments, retrieve):
    import os
    from pathlib import Path
    import time
    from uuid import uuid4
    probe = Path(os.environ["RECOVERY_TEST_PROBE"])
    (probe / ("sse-search-" + str(os.getpid()) + "-" + uuid4().hex)).touch()
    deadline = time.monotonic() + 60
    while not (probe / "release-join").exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("未释放本机断线屏障")
        time.sleep(.04)
    return _original_search(arguments, retrieve)
''', encoding="utf-8")
    env = environment(tmp_path, scenes)
    env["RECOVERY_TEST_PROBE"] = str(probe)
    with ExitStack() as stack:
        connection = client(stack, env)
        api = start_api(stack, ROOT, env, tmp_path, "api-sse", connection)
        body = {"business_id": scene_id, "message": "项目简报" if durable else "会议纪要",
                "execution_mode": execution_mode, "response_mode": "streaming", "mode": "offline"}
        initial = []
        with connection.stream("POST", API + "/chat", json=body) as response:
            response.raise_for_status()
            assert response.headers["content-type"].startswith("text/event-stream")
            for frame in frames(response.iter_lines()):
                initial.append(frame)
                if frame["event"] == "tool_started":
                    break
            entered = wait_until(lambda: list(probe.glob("sse-compose-*" if durable else "sse-search-*")),
                                 [api], "持久物理调用探针")
            assert len(entered) == 1 and f"-{api[0].pid}-" in entered[0].name
        # 已消费完整 tool_started 帧并真正关闭 HTTP 流，业务任务仍在外部屏障内。
        tasks = connection.get(API + "/tasks").raise_for_status().json()["tasks"]
        assert len(tasks) == 1 and tasks[0]["status"] == "running"
        path = API + "/tasks/" + tasks[0]["task_id"]
        (probe / "release-join").touch()
        done = wait_until(lambda: finished(connection, path), [api], "断线后任务自行完成")
        expected_count = 3 if durable else 1
        assert len(done["tool_calls"]) == expected_count
        physical = sorted(p.name for p in probe.glob("sse-*"))
        assert len(physical) == expected_count
        if durable:
            assert all(len(list(probe.glob("sse-" + step + "-*"))) == 1 for step in ("progress", "risks", "compose"))

        # 第二条网络流只消费一帧即断开，第三条从该帧的实际持久 ID 继续。
        with connection.stream("GET", path + "/events", headers={"Last-Event-ID": str(initial[-1]["id"])}) as response:
            response.raise_for_status()
            middle = next(frames(response.iter_lines()))
        tail = replay(connection, path, middle["id"])
        persisted = connection.get(path + "/trace").raise_for_status().json()["events"]
        actual = initial + [middle] + tail
        expected = [{key: item[key] for key in ("id", "event", "data")} for item in persisted]
        assert actual == expected
        ids = [item["id"] for item in actual]
        assert ids == sorted(set(ids))
        assert [item["event"] for item in actual if item["event"] in TERMINALS] == ["task_completed"]
        assert replay(connection, path, ids[-1]) == []
        assert replay(connection, path, initial[-1]["id"]) == [item for item in expected if item["id"] > initial[-1]["id"]]
        assert physical == sorted(p.name for p in probe.glob("sse-*"))
        assert connection.get(path).json() == done


def export_baseline(directory):
    """只从固定已审基线导出源码，不复原数据库或运行环境。"""
    directory.mkdir()
    archive = subprocess.check_output(["git", "-C", str(ROOT), "archive", BASELINE])
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        source.extractall(directory, filter="data")
    assert (directory / "run.py").is_file()


def test_code_and_configuration_rollback_preserve_history(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    scenes = state / "scenarios"
    scene = copy_scene(ROOT / "scenarios/notes", scenes / "notes")
    probe = state / "physical"
    probe.mkdir()
    # 发布门禁也会执行适配器；只在历史读取前后比较探针，避免把合法评测误计为重放。
    candidate = copy_scene(scene, state / "candidates/vnext/notes")
    manifest_path = candidate / "scenario.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    stable_version = manifest["version"]
    manifest["version"] = "1.1.0"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    docs = candidate / "notes.json"
    documents = json.loads(docs.read_text(encoding="utf-8"))
    documents[0]["snippet"] += " P4-CANDIDATE"
    docs.write_text(json.dumps(documents, ensure_ascii=False), encoding="utf-8")
    # 固定主体和精确散列校验，灰度覆盖不依靠随机抽样。
    actors = []
    for user, variable in (("fictional-canary", "AGENT_ACCESS_TOKEN"), ("fictional-stable", "P4_STABLE_TOKEN")):
        actors.append({"token_env": variable, "tenant_id": "p4-fictional", "org_id": "local", "user_id": user,
                       "grants": {"notes": {"permissions": ["knowledge.read", "notes.read"]}}})
    buckets = [int.from_bytes(hashlib.sha256(json.dumps([actor["tenant_id"], actor["org_id"], actor["user_id"],
                                                        "notes", ""], ensure_ascii=True).encode()).digest()[:8], "big") % 100
               for actor in actors]
    if buckets[0] > buckets[1]:
        actors[0]["token_env"], actors[1]["token_env"] = actors[1]["token_env"], actors[0]["token_env"]
    assert len(set(buckets)) == 2
    percent = min(buckets) + 1
    principals = state / "principals.json"
    principals.write_text(json.dumps(actors), encoding="utf-8")
    policy = state / "p2.json"
    policy.write_text(json.dumps({"results": {"keys": {"local": "P4_RESULT_KEY"}, "active_key": "local", "page_bytes": 512}}), encoding="utf-8")
    env = environment(state, scenes)
    env.update(AGENT_PRINCIPALS_FILE=str(principals), AGENT_RELEASE_CANDIDATES=str(state / "candidates"),
               AGENT_P2_POLICY=str(policy), P4_RESULT_KEY="42" * 32,
               P4_STABLE_TOKEN="p4-fictional-stable", P4_RELEASE_PROBE=str(probe))
    package = tmp_path / "lookup"
    package.mkdir()
    (package / "SKILL.md").write_text("---\nname: lookup\ndescription: 本机查询\n---\n保留来源证据。", encoding="utf-8")
    registry = SkillRegistry(state / "skills")
    registry.import_package(package, author="fictional-writer")
    old_root = tmp_path / "baseline"
    export_baseline(old_root)
    records = []
    stable_headers = {"Authorization": "Bearer p4-fictional-stable"}
    skill_body = {"business_id": "notes", "name": "lookup", "version": "1.0.0"}

    with ExitStack() as stack:
        connection = client(stack, env)

        def boot(root, label):
            return start_api(stack, root, env, state, label, connection, "-c", COUNTED_API)

        active = boot(ROOT, "candidate-api")

        def admin(path, body):
            response = connection.post(ADMIN + path, headers=PUBLISHER, json=body)
            assert response.is_success, response.text
            return response.json()

        # 笔记适配器会走受控资料检索，冻结技能须明确允许该依赖。
        admin("/skills/validate", skill_body | {"tools": ["notes_search", "retrieve_knowledge"]})
        admin("/skills/stage", skill_body)
        governance = admin("/skills/activate", skill_body)
        assert governance["assurance"] == "local-hmac-sha256"
        operator_key = (state / "skills/.operator-key").read_bytes()

        def submit(label, headers=None):
            headers = (headers or {}) | {"Idempotency-Key": label}
            response = connection.post(API + "/chat", json=BODY, headers=headers)
            assert response.status_code == 200, response.text
            task = response.json()
            assert task["status"] == "completed", json.dumps(task, ensure_ascii=False)
            assert ("P4-CANDIDATE" in task["answer"]) == (task["business_version"] == "1.1.0")
            path = API + "/tasks/" + task["task_id"]
            items = connection.get(path + "/results", headers=headers).raise_for_status().json()["results"]
            ref = items[0]["result_ref"]
            page = result_pages(ref, headers)
            events = replay_for(headers, path)
            records.append((label, headers, task, ref, page, events))
            return task

        def replay_for(headers, path):
            response = connection.get(path + "/events", headers=headers).raise_for_status()
            return list(frames(response.iter_lines()))

        def result_pages(ref, headers):
            pages = []
            cursor = None
            while True:
                response = connection.get(API + "/results/" + ref + "/pages", headers=headers,
                                          params={"cursor": cursor} if cursor else {})
                page = response.raise_for_status().json()
                pages.append(page)
                cursor = page["next_cursor"]
                if cursor is None:
                    return pages

        assert submit("before-release")["business_version"] == stable_version
        route = "/releases/" + admin("/releases", {"business_id": "notes", "candidate": "vnext"})["release_id"]
        admin(route + "/activate", {"canary_percent": percent})
        assert submit("canary-new")["business_version"] == "1.1.0"
        assert submit("canary-stable", stable_headers)["business_version"] == stable_version
        admin(route + "/activate", {"canary_percent": 100})
        assert submit("activated", stable_headers)["business_version"] == "1.1.0"
        admin(route + "/rollback", {})
        assert submit("after-config-rollback")["business_version"] == stable_version
        # 删除和过期是当前权威状态；随后正常回退程序不可复活这些密文引用。
        deleted_ref = records[0][3]
        assert connection.delete(API + "/results/" + deleted_ref).raise_for_status().json()["deleted"]
        kill(active[0])
        expired_ref = records[1][3]
        with sqlite3.connect(state / "data.db") as db:
            # 停写夹具推进单一结果的截止时间，同时重新认证元数据，避免把密文损坏冒充过期。
            nonce, ciphertext, metadata = db.execute(
                "SELECT nonce,ciphertext,metadata FROM tool_results WHERE result_ref=?", (expired_ref,)).fetchone()
            cipher = AESGCM(bytes.fromhex(env["P4_RESULT_KEY"]))
            payload = cipher.decrypt(nonce, ciphertext, metadata.encode())
            metadata = json.loads(metadata)
            metadata["expires_at"] = time.time() - 1
            encoded = json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            nonce = os.urandom(12)
            db.execute("UPDATE tool_results SET expires_at=?,nonce=?,ciphertext=?,metadata=? WHERE result_ref=?",
                       (metadata["expires_at"], nonce, cipher.encrypt(nonce, payload, encoded.encode()), encoded, expired_ref))
            audit_before = db.execute("SELECT * FROM audit_logs ORDER BY rowid").fetchall()

        def check_history():
            before = sorted(p.name for p in probe.iterdir())
            for label, headers, task, ref, page, events in records:
                path = API + "/tasks/" + task["task_id"]
                assert connection.get(path, headers=headers).raise_for_status().json() == task
                assert replay_for(headers, path) == events
                duplicate = connection.post(API + "/chat", headers=headers, json=BODY).raise_for_status().json()
                assert duplicate == task
                response = connection.get(API + "/results/" + ref + "/pages", headers=headers)
                if ref in {deleted_ref, expired_ref}:
                    assert response.status_code == 404
                else:
                    assert result_pages(ref, headers) == page
                assert [item["event"] for item in events if item["event"] in TERMINALS] == ["task_completed"]
            assert sorted(p.name for p in probe.iterdir()) == before
            assert connection.get(ADMIN + "/skills/notes/lookup/1.0.0", headers=PUBLISHER).raise_for_status().json() == governance
            assert (state / "skills/.operator-key").read_bytes() == operator_key
            with sqlite3.connect(state / "data.db") as db:
                assert db.execute("SELECT * FROM audit_logs ORDER BY rowid LIMIT ?", (len(audit_before),)).fetchall() == audit_before
                assert db.execute("SELECT COUNT(*) FROM idempotency").fetchone()[0] == len(records)
                assert db.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == len(records)
                # Store._finish 对所有已完成任务清除工具缓存；保留调用记录和独立密文结果。
                calls = db.execute("SELECT task_id,execution_id,status,result FROM calls").fetchall()
                assert {row[0] for row in calls} == {record[2]["task_id"] for record in records}
                assert all(row[2:] == ("completed", None) for row in calls)
                retained = db.execute("SELECT result_ref,metadata,nonce,ciphertext FROM tool_results").fetchall()
                assert {row[0] for row in retained} == {record[3] for record in records} - {deleted_ref, expired_ref}
                assert all(json.loads(row[1])["key_id"] == "local" and len(row[2]) == 12
                           and isinstance(row[3], bytes) and len(row[3]) > 16 for row in retained)

        # 相同代码先重启，再真正换到固定旧源码目录；没有旧数据库覆盖。
        active = boot(ROOT, "candidate-restarted")
        check_history()
        kill(active[0])
        active = boot(old_root, "baseline-api")
        check_history()
        assert submit("baseline-created")["business_version"] == stable_version
        kill(active[0])
        active = boot(ROOT, "candidate-reads-baseline")
        check_history()
        # 热改角色文件后立即拒绝；保持撤权状态跨代码切换。
        for actor in actors:
            actor["grants"]["notes"]["permissions"].remove("notes.read")
        principals.write_text(json.dumps(actors), encoding="utf-8")

        def denied(permission=True):
            _, headers, task, ref, _, _ = records[-1]
            path = API + "/tasks/" + task["task_id"]
            for url in (path, path + "/events"):
                assert connection.get(url, headers=headers).status_code == (403 if permission else 200)
            for url in (API + "/results/" + ref, API + "/results/" + ref + "/pages"):
                assert connection.get(url, headers=headers).status_code == 403

        denied()
        kill(active[0])
        active = boot(ROOT, "candidate-denied")
        denied()
        # 恢复角色也不能绕过持久化工具吊销。
        for actor in actors:
            actor["grants"]["notes"]["permissions"].append("notes.read")
        principals.write_text(json.dumps(actors), encoding="utf-8")
        admin("/capabilities/notes_search/revoke", {"business_id": "notes"})
        denied(permission=False)
        kill(active[0])
        active = boot(old_root, "baseline-revoked")
        denied(permission=False)
        # 全部写入者停止后备份完整状态，恢复另一个目录和端口；原状态不被覆盖。
        kill(active[0])
        restored = tmp_path / "restored"
        backup_state(state, restored)
        restored_env = environment(restored, restored / "scenarios")
        restored_env.update({key: value for key, value in env.items() if key.startswith("P4_")})
        restored_env.update(AGENT_PRINCIPALS_FILE=str(restored / "principals.json"),
                            AGENT_RELEASE_CANDIDATES=str(restored / "candidates"), AGENT_P2_POLICY=str(restored / "p2.json"),
                            P4_RELEASE_PROBE=str(restored / "physical"))
        restored_client = client(stack, restored_env)
        start_api(stack, old_root, restored_env, restored, "restored-api", restored_client, "-c", COUNTED_API)
        assert (restored / "skills/.operator-key").read_bytes() == operator_key
        assert restored_client.get(ADMIN + "/skills/notes/lookup/1.0.0", headers=PUBLISHER).raise_for_status().json() == governance
        for _, headers, task, ref, _, _ in records:
            assert restored_client.get(API + "/tasks/" + task["task_id"], headers=headers).raise_for_status().json() == task
            assert restored_client.get(API + "/results/" + ref + "/pages", headers=headers).status_code in (403, 404)
        assert sorted(p.name for p in (restored / "physical").iterdir()) == sorted(p.name for p in probe.iterdir())
