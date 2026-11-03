"""Run a disposable local approval/release demo. --verify exercises the public HTTP APIs."""
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
EXAMPLE = ROOT / "examples" / "governance"
API = "/api/v1/agent"
ADMIN = "/api/v1/agent-admin"


def wait_for(read, ready, timeout=30):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = read()
        if ready(value):
            return value
        time.sleep(.08)
    raise RuntimeError("Timed out waiting for the local service")


def verify(client, owner, reviewer, publisher):
    def call(method, route, token, *, body=None, key=None):
        headers = {"Authorization": "Bearer " + token}
        if key:
            headers["Idempotency-Key"] = key
        response = client.request(method, route, headers=headers, json=body)
        if not response.is_success:
            raise RuntimeError(f"{method} {route}: {response.status_code} {response.text}")
        return response.json()

    def drive(expected_version):
        response = call("POST", API + "/chat", owner, key=secrets.token_hex(8), body={
            "business_id": "change_requests", "execution_mode": "durable_plan",
            "message": "提交测试草案", "context": {"object_ids": ["draft-1"]}})
        if response["business_version"] != expected_version:
            raise RuntimeError("Task was admitted on an unexpected version")
        tid = response["task_id"]
        url = API + "/tasks/" + tid
        approvals = wait_for(lambda: call("GET", API + "/approvals?status=requested", reviewer)["approvals"],
                             lambda rows: any(row["task_id"] == tid for row in rows))
        a = next(row for row in approvals if row["task_id"] == tid)
        body = {"decision": "approve", "proposal_digest": a["proposal_digest"]}
        route = API + "/approvals/" + a["approval_id"]
        key = secrets.token_hex(8)
        for _ in range(2):
            call("POST", route + "/decision", reviewer, key=key, body=body)
        final = wait_for(lambda: call("GET", url, owner), lambda t: t["status"] in ("completed", "failed"))
        if final["status"] != "completed" or len(final["tool_calls"]) != 1:
            raise RuntimeError("Mock commit did not finish exactly once")
        approval = call("GET", route, reviewer)
        if approval["commit_status"] != "succeeded" or not approval["receipt_id"]:
            raise RuntimeError("Missing verified mock receipt")
        spans = call("GET", ADMIN + "/observability/traces/" + tid, publisher)
        if not spans["resourceSpans"]:
            raise RuntimeError("Trace export is empty")
        return {"task_id": tid, "release_version": final["business_version"],
                "receipt_id": approval["receipt_id"], "new_object_version": a["field_diff"]["version"]["after"]}

    results = [drive("1.0.0")]
    stage = call("POST", ADMIN + "/releases", publisher, body={"business_id": "change_requests", "candidate": "vnext"})
    release = ADMIN + "/releases/" + stage["release_id"]
    call("POST", release + "/activate", publisher, body={"canary_percent": 10})
    call("POST", release + "/activate", publisher, body={"canary_percent": 100})
    results.append(drive("1.1.0"))
    call("POST", release + "/rollback", publisher, body={})
    results.append(drive("1.0.0"))
    if [t["new_object_version"] for t in results] != [1, 2, 3]:
        raise RuntimeError("Duplicate or skipped ledger update")
    call("GET", ADMIN + "/observability/metrics", publisher)
    return {"environment": "offline-loopback", "runs": results, "candidate_gate": stage["evaluation"]["passed"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true", help="run the acceptance scenario and exit")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as folder:
        work = Path(folder)
        scene = "change_requests"
        scenarios = work / "project" / "scenarios"
        shutil.copytree(EXAMPLE / "scenarios", scenarios, ignore=shutil.ignore_patterns("__pycache__"))
        candidate = work / "candidates" / "vnext" / scene
        shutil.copytree(scenarios / scene, candidate)
        manifest = candidate / "scenario.json"
        data = json.loads(manifest.read_text(encoding="utf-8"))
        data["version"] = "1.1.0"
        manifest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        owner, reviewer, publisher = (secrets.token_urlsafe(32) for _ in range(3))
        env = {**os.environ, "AGENT_SCENARIOS": str(scenarios), "AGENT_DATABASE": str(work / "local.db"),
               "AGENT_PRINCIPALS_FILE": str(EXAMPLE / "principals.json"), "AGENT_MOCK_WRITES": "1",
               "AGENT_ACCESS_TOKEN": owner, "AGENT_APPROVER_TOKEN": reviewer, "AGENT_ADMIN_TOKEN": publisher,
               "AGENT_SERVICE_TOKEN": "", "AGENT_RELEASE_CANDIDATES": str(work / "candidates"), "AGENT_PORT": str(port),
               "AGENT_MODEL_API_KEY": "", "AGENT_MODEL_NAME": "", "AGENT_MODEL_BASE_URL": "",
               "AGENT_PLAN_WORKER": "embedded", "AGENT_PLAN_LEASE_SECONDS": "12", "AGENT_REAL_WRITES": "0"}
        log = work / "server.log"
        with log.open("wb") as stream:
            process = subprocess.Popen([sys.executable, "run.py"], cwd=ROOT, env=env,
                                       stdout=stream, stderr=subprocess.STDOUT)
            try:
                with httpx.Client(base_url=url, trust_env=False, timeout=40) as client:
                    def ready():
                        if process.poll() is not None:
                            raise RuntimeError(log.read_text(encoding="utf-8", errors="replace")[-1500:])
                        try:
                            return client.get("/openapi.json", timeout=1).is_success
                        except httpx.TransportError:
                            return False
                    wait_for(ready, bool)
                    if args.verify:
                        print(json.dumps(verify(client, owner, reviewer, publisher), ensure_ascii=False, indent=2))
                        return
                    print(f"申请工作台 {url}/\n申请人令牌 {owner}\n\n独立审批页 {url}/review\n审批人令牌 {reviewer}\n"
                          f"\n管理 API {url}{ADMIN}\n发布管理员令牌 {publisher}\n\n"
                          "这些临时令牌仅用于本次本地演示。按 Ctrl+C 退出并清理演示数据。", flush=True)
                    process.wait()
            except KeyboardInterrupt:
                pass
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()


if __name__ == "__main__":
    main()
