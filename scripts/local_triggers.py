"""启动临时回环服务，以真实 HTTP 验收事件与定时任务，不接入生产事件系统。"""
import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.triggers import sign  # noqa: E402


def wait_for(read, ready, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = read()
        if ready(value):
            return value
        time.sleep(.1)
    raise RuntimeError("本地触发验收等待超时")


def verify(client, actor, signing_key):
    now = int(time.time())
    event = {"event_id": "local-event-1", "business_id": "project_brief", "message": "事件触发的项目简报",
             "context": {}, "issued_at": now, "expires_at": now + 120, "execution_mode": "durable_plan"}
    raw = json.dumps(event, ensure_ascii=False).encode()
    timestamp, nonce = str(now), secrets.token_hex(16)
    headers = {"X-Trigger-Source": "local-demo", "X-Trigger-Timestamp": timestamp,
               "X-Trigger-Nonce": nonce, "Content-Type": "application/json",
               "X-Trigger-Signature": sign(signing_key, "local-demo", timestamp, nonce, raw)}
    route = "/internal/agent/triggers/events"
    first = client.post(route, content=raw, headers=headers)
    first.raise_for_status()
    assert first.status_code == 202
    receipt = first.json()
    duplicate = client.post(route, content=raw, headers=headers)
    assert duplicate.status_code == 200 and duplicate.json()["task_id"] == receipt["task_id"]
    forged = client.post(route, content=raw + b" ", headers=headers)
    assert forged.status_code == 401
    actor_headers = {"Authorization": "Bearer " + actor}

    def task():
        response = client.get("/api/v1/agent/tasks/" + receipt["task_id"], headers=actor_headers)
        response.raise_for_status()
        return response.json()

    completed = wait_for(task, lambda value: value["status"] in {"completed", "failed"})
    assert completed["status"] == "completed" and completed["execution_mode"] == "durable_plan"

    def scheduled_tasks():
        response = client.get("/api/v1/agent/tasks", headers=actor_headers)
        response.raise_for_status()
        return [t for t in response.json()["tasks"] if t["message"] == "定时触发的项目简报" and t["status"] == "completed"]

    scheduled = wait_for(scheduled_tasks, bool)
    assert len(scheduled) == 1
    return {"passed": True, "event_task_id": receipt["task_id"], "schedule_task_id": scheduled[0]["task_id"],
            "event_deduplicated": True, "forged_signature_rejected": True,
            "durable_plan_completed": True, "production_integration": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true", help="兼容其他本地演示的验收参数")
    parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="agent-triggers-") as temporary:
        work = Path(temporary)
        scenes = work / "scenarios"
        shutil.copytree(ROOT / "examples" / "durable" / "scenarios", scenes,
                        ignore=shutil.ignore_patterns("__pycache__"))
        actor, signing_key = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        config = json.loads((ROOT / "examples" / "triggers" / "triggers.json").read_text(encoding="utf8"))
        config["schedules"] = [{"schedule_id": "local-summary", "source_id": "local-demo",
                                "business_id": "project_brief", "message": "定时触发的项目简报", "context": {},
                                "start_at": int(time.time()) + 3, "interval_seconds": 3600}]
        config_path = work / "triggers.json"
        config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf8")
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_")}
        env.update(AGENT_PORT=str(port), AGENT_ACCESS_TOKEN=actor, AGENT_TRIGGER_ACTOR_TOKEN=actor,
                   AGENT_TRIGGER_SIGNING_KEY=signing_key, AGENT_SCENARIOS=str(scenes),
                   AGENT_DATABASE=str(work / "data.db"), AGENT_TRIGGERS_FILE=str(config_path),
                   AGENT_PRINCIPALS_FILE=str(ROOT / "examples" / "triggers" / "principals.json"),
                   AGENT_PLAN_WORKER="embedded", AGENT_SKILL_ROOT=str(work / "skills"),
                   PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
        log = work / "server.log"
        with log.open("wb") as stream:
            process = subprocess.Popen([sys.executable, "run.py"], cwd=ROOT, env=env,
                                       stdout=stream, stderr=subprocess.STDOUT,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            try:
                with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=15) as client:
                    def ready():
                        if process.poll() is not None:
                            raise RuntimeError(log.read_text(encoding="utf8", errors="replace")[-2000:])
                        try:
                            return client.get("/openapi.json", timeout=1).is_success
                        except httpx.TransportError:
                            return False
                    wait_for(ready, bool)
                    print(json.dumps(verify(client, actor, signing_key), ensure_ascii=False, indent=2))
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=8)


if __name__ == "__main__":
    main()
