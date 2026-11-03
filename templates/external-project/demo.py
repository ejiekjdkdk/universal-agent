"""使用真实 HTTP 验证复制到仓库外的项目能接入既有底座。"""
import argparse
from contextlib import ExitStack, contextmanager
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

PROJECT = Path(__file__).resolve().parent
QUESTION = "请查询 DOC-101 的状态"


def ports():
    with socket.socket() as project, socket.socket() as agent:
        project.bind(("127.0.0.1", 0))
        agent.bind(("127.0.0.1", 0))
        return project.getsockname()[1], agent.getsockname()[1]


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


@contextmanager
def service(script, env, log):
    with log.open("wb") as stream:
        process = subprocess.Popen([sys.executable, str(script)], cwd=script.parent, env=env,
                                   stdout=stream, stderr=subprocess.STDOUT)
        try:
            yield process
        finally:
            stop(process)


def wait_ready(client, process, url, log):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(log.read_text(encoding="utf-8", errors="replace")[-2000:])
        try:
            if client.get(url + "/openapi.json", timeout=1).is_success:
                return
        except httpx.RequestError:
            pass
        time.sleep(0.1)
    raise RuntimeError(f"Service not ready at {url}")


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def remove_workspace(folder):
    # Windows 虚拟环境启动器退出后，实际子进程可能仍在释放数据库文件。
    deadline = time.monotonic() + 5
    while True:
        try:
            shutil.rmtree(folder)
            return
        except PermissionError as error:
            # 仅等待文件共享冲突；其他清理错误或持续占用必须报告给调用者。
            if getattr(error, "winerror", None) != 32 or time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def verify(agent_root):
    require((agent_root / "run.py").is_file(), "--agent-root must contain run.py")
    require(not PROJECT.is_relative_to(agent_root) and not agent_root.is_relative_to(PROJECT),
            "Copy this template outside the agent repository before running this check.")
    project_port, agent_port = ports()
    project_token, agent_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    project_url = f"http://127.0.0.1:{project_port}"
    agent_url = f"http://127.0.0.1:{agent_port}"
    project_auth = {"Authorization": "Bearer " + project_token}
    agent_auth = {"Authorization": "Bearer " + agent_token}
    with ExitStack() as stack:
        folder = tempfile.mkdtemp()
        stack.callback(remove_workspace, folder)
        scratch = Path(folder)
        env = {**os.environ, "PROJECT_API_PORT": str(project_port), "AGENT_PORT": str(agent_port),
               "PROJECT_API_TOKEN": project_token, "AGENT_ACCESS_TOKEN": agent_token,
               "AGENT_SCENARIOS": str(PROJECT / "scenarios"), "AGENT_DATABASE": str(scratch / "agent.db"),
               "AGENT_MODEL_BASE_URL": "", "AGENT_MODEL_NAME": "", "AGENT_MODEL_API_KEY": "",
               "AGENT_SERVICE_TOKEN": "", "PYTHONIOENCODING": "utf-8"}
        project_log, agent_log = scratch / "project.log", scratch / "agent.log"
        project = stack.enter_context(service(PROJECT / "project_api.py", env, project_log))
        agent = stack.enter_context(service(agent_root / "run.py", env, agent_log))
        client = stack.enter_context(httpx.Client(trust_env=False, timeout=15))
        wait_ready(client, project, project_url, project_log)
        wait_ready(client, agent, agent_url, agent_log)

        listing = client.get(agent_url + "/api/v1/agent/scenarios", headers=agent_auth)
        listing.raise_for_status()
        require([s["id"] for s in listing.json()["scenarios"]] == ["project_records"],
                "Agent did not load the external catalog in isolation")
        denied = client.get(project_url + "/v1/records/DOC-101", headers=agent_auth)
        require(denied.status_code == 401, "Project accepted the agent access token")

        headers = {**project_auth, "Idempotency-Key": secrets.token_hex(12)}
        def ask(message, request_headers):
            response = client.post(project_url + "/ask", headers=request_headers, json={"message": message})
            response.raise_for_status()
            return response.json()

        task = ask(QUESTION, headers)
        expected = json.loads((PROJECT / "records.json").read_text(encoding="utf-8"))["DOC-101"]
        require(task["status"] == "completed" and expected["status"] in task["answer"],
                "Answer does not reflect the external project's data")
        evidence = [{"source_id": "DOC-101", "title": expected["title"],
                     "snippet": expected["status"], "version": expected["version"]}]
        require(task["evidence"] == evidence, "Evidence does not match the project record")
        require(len(task["tool_calls"]) == 1, "Expected exactly one tool execution")
        call = task["tool_calls"][0]
        require(call["capability_id"] == "lookup_record" and call["status"] == "completed", "Wrong tool")
        trace = task["trace"]
        require(trace[0]["event"] == "task_started" and trace[-1]["event"] == "task_completed",
                "Incomplete persisted trace")
        require(any(event["event"] == "tool_result" and event["data"]["execution_id"] == call["execution_id"]
                    and event["data"]["source_ids"] == ["DOC-101"] for event in trace),
                "Trace does not link the execution to the project source")
        replay = ask(QUESTION, headers)
        same_task = replay["task_id"] == task["task_id"]
        require(same_task, "Idempotent retry created a second task")
        missing = ask("请查询 DOC-999999", project_auth)
        require(missing["status"] == "completed" and not missing["evidence"], "Missing record produced evidence")

        stop(project)
        reads = project_log.read_text(encoding="utf-8", errors="replace").count('GET /v1/records/DOC-101 HTTP/1.1" 200')
        require(reads == 1, f"Expected one successful live read; project access log contained {reads}")
        unavailable = client.post(agent_url + "/api/v1/agent/chat", headers=agent_auth,
                                  json={"business_id": "project_records", "message": QUESTION, "mode": "offline"})
        unavailable.raise_for_status()
        failed = unavailable.json()
        require(failed["status"] == "failed" and failed["error"] and not failed["evidence"],
                "Agent did not report loss of the upstream service")
        return {"task_id": task["task_id"], "answer": task["answer"], "tool": call["capability_id"],
                "source_id": evidence[0]["source_id"], "trace_events": [row["event"] for row in trace],
                "checks": {"project_api_calls": reads, "idempotent_replay": same_task,
                           "unauthorized_record_read": denied.status_code, "upstream_offline": failed["status"],
                           "upstream_error": failed["error"]["code"]}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Smoke test a copied project against an unmodified agent checkout, using real HTTP."
    )
    parser.add_argument("--agent-root", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.agent_root.resolve()), ensure_ascii=False, indent=2))
