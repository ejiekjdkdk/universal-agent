"""在汇总步骤强制终止 Worker，验证接手进程使用原任务的版本快照。"""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
from uuid import uuid4

import httpx


ROOT = Path(__file__).resolve().parents[1]
SCENARIO = ROOT / "examples" / "durable" / "scenarios" / "project_brief"
API = "/api/v1/agent"

# 每次调用单独写一个文件，进程退出后仍能核对实际调用次数。
# 控制汇总步骤的标记放在场景包外，不参与版本快照。
ADAPTER = '''
import os
from pathlib import Path
import time
from uuid import uuid4

RELEASE = {release!r}
PROBE = Path(os.environ["RECOVERY_TEST_PROBE"])


def record(step):
    (PROBE / f"{{RELEASE}}-{{step}}-{{os.getpid()}}-{{uuid4().hex}}").touch()


def progress(arguments, retrieve):
    record("progress")
    hits = retrieve("项目进度", 1)
    return {{"data": {{"summary": hits[0]["snippet"], "count": len(hits)}}, "evidence": hits}}


def risks(arguments, retrieve):
    record("risks")
    hits = retrieve("项目风险", 1)
    return {{"data": {{"summary": hits[0]["snippet"], "count": len(hits)}}, "evidence": hits}}


def compose(arguments, retrieve):
    record("compose")
    deadline = time.monotonic() + 60
    while not (PROBE / "release-join").exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("test did not release the join")
        time.sleep(0.04)
    summary = RELEASE + ": " + arguments["progress"] + " / " + arguments["risks"]
    return {{"data": {{"summary": summary}}, "evidence": []}}
'''


def publish(scene, version, label):
    manifest_path = scene / "scenario.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["version"] = version
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    (scene / "adapter.py").write_text(ADAPTER.format(release=label), encoding="utf-8")


def kill(process):
    if process.poll() is None:
        process.kill()
        process.wait(timeout=10)


def wait_until(check, processes, description, timeout=40):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        for process, log in processes:
            if process.poll() is not None:
                raise AssertionError(f"{description}: process exited {process.returncode}\n"
                                     + log.read_text(encoding="utf-8", errors="replace")[-4000:])
        value = check()
        if value:
            return value
        time.sleep(0.06)
    raise AssertionError(f"Timed out waiting for {description}")


def test_worker_killed_during_join_resumes_only_uncommitted_read_and_pins_release(tmp_path):
    scenes = tmp_path / "scenarios"
    scene = shutil.copytree(SCENARIO, scenes / "project_brief", ignore=shutil.ignore_patterns("__pycache__"))
    probe = tmp_path / "probe"
    probe.mkdir()
    publish(scene, "1.0.0", "original")
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]

    token = uuid4().hex
    env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_")}
    env.update(AGENT_PORT=str(port), AGENT_ACCESS_TOKEN=token, AGENT_SCENARIOS=str(scenes),
               AGENT_DATABASE=str(tmp_path / "data.db"), AGENT_PLAN_WORKER="external",
               AGENT_PLAN_LEASE_SECONDS="2", RECOVERY_TEST_PROBE=str(probe),
               PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")

    with ExitStack() as stack:
        def launch(label, *args):
            log = tmp_path / (label + ".log")
            output = stack.enter_context(log.open("wb"))
            proc = subprocess.Popen([sys.executable, *args], cwd=ROOT, env=env,
                                    stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            stack.callback(kill, proc)
            return proc, log

        client = stack.enter_context(httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False,
                                                  headers={"Authorization": f"Bearer {token}"}, timeout=5))

        def start_api(label):
            proc = launch(label, "run.py")
            def ready():
                try:
                    return client.get(API + "/scenarios").is_success
                except httpx.TransportError:
                    return False
            wait_until(ready, [proc], "API startup")
            return proc

        def submit():
            response = client.post(API + "/chat", json={"business_id": "project_brief", "message": "项目简报",
                                                        "execution_mode": "durable_plan", "mode": "offline"})
            assert response.status_code == 202, response.text
            return API + "/tasks/" + response.json()["task_id"]

        def finished(path):
            task = client.get(path).raise_for_status().json()
            if task["status"] in ("failed", "partial", "cancelled"):
                raise AssertionError(f"Unexpected task terminal: {task}")
            return task if task["status"] == "completed" else None

        first_api = start_api("api-original")
        old = submit()
        old_plan = client.get(old + "/plan").raise_for_status().json()
        first_worker = launch("worker-original", "-m", "app.worker")
        entered = wait_until(lambda: list(probe.glob("original-compose-*")),
                             [first_api, first_worker], "first join call")
        assert len(entered) == 1 and entered[0].name.startswith(f"original-compose-{first_worker[0].pid}-")
        steps = client.get(old + "/steps").raise_for_status().json()["steps"]
        assert {s["step_id"]: s["status"] for s in steps} == {
            "progress": "succeeded", "risks": "succeeded", "brief": "running"}

        # 强制退出并重启 API，确保接手方只能依赖持久化状态恢复。
        kill(first_worker[0])
        kill(first_api[0])
        publish(scene, "2.0.0", "updated-release")
        (probe / "release-join").touch()

        second_api = start_api("api-updated")
        workers = [launch("worker-" + label, "-m", "app.worker") for label in ("left", "right")]
        active = [second_api, *workers]
        resumed = wait_until(lambda: finished(old), active, "resumed task")
        assert resumed["answer"].startswith("original:")
        assert resumed["evidence"]
        assert old_plan["snapshot_id"] == client.get(old + "/plan").raise_for_status().json()["snapshot_id"]
        attempts = client.get(old + "/steps").raise_for_status().json()["steps"]
        assert {s["step_id"]: s["attempt_no"] for s in attempts} == {"progress": 1, "risks": 1, "brief": 2}
        assert all(s["status"] == "succeeded" for s in attempts)
        for step, count in (("progress", 1), ("risks", 1), ("compose", 2)):
            assert len(list(probe.glob(f"original-{step}-*"))) == count
        assert sum(len(list(probe.glob(f"original-compose-{p.pid}-*"))) for p, _ in workers) == 1
        replay = client.get(old + "/events").raise_for_status().text
        terminals = [line for line in replay.splitlines()
                     if line in ("event: task_completed", "event: task_failed", "event: task_cancelled")]
        assert terminals == ["event: task_completed"]

        new = submit()
        new_result = wait_until(lambda: finished(new), active, "new release task")
        assert new_result["answer"].startswith("updated-release:")
        assert new_result["business_version"] == "2.0.0"
        assert old_plan["snapshot_id"] != client.get(new + "/plan").raise_for_status().json()["snapshot_id"]
        assert all(len(list(probe.glob(f"updated-release-{step}-*"))) == 1
                   for step in ("progress", "risks", "compose"))
        assert all(p.poll() is None for p, _ in workers)
