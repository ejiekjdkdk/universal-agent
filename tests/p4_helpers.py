"""P4 回环服务验收的进程、流帧和停写备份辅助。"""
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys

import httpx

from test_worker_recovery import kill, wait_until


def launch(stack, root, env, directory, label, *args):
    """直接启动真实解释器，先杀进程并等待，再关闭日志。"""
    environment = dict(env)
    executable = sys.executable
    paths = [str(root)]
    if os.name == "nt" and sys.prefix != sys.base_prefix:
        executable = sys._base_executable
        paths.append(str(Path(sys.prefix) / "Lib/site-packages"))
    environment["PYTHONPATH"] = os.pathsep.join(paths)
    log = directory / (label + ".log")
    output = stack.enter_context(log.open("wb"))
    process = subprocess.Popen([executable, *args], cwd=root, env=environment,
                               stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    stack.callback(kill, process)
    return process, log


def environment(directory, scenes):
    """丢弃外部业务配置，只允许当前测试的虚构输入。"""
    env = {key: value for key, value in os.environ.items() if not key.startswith("AGENT_")}
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    env.update(AGENT_PORT=str(port), AGENT_ACCESS_TOKEN="p4-fictional-owner",
               AGENT_ADMIN_TOKEN="p4-fictional-admin", AGENT_SCENARIOS=str(scenes),
               AGENT_DATABASE=str(directory / "data.db"), AGENT_SKILL_ROOT=str(directory / "skills"),
               PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    return env


def client(stack, env):
    return stack.enter_context(httpx.Client(base_url="http://127.0.0.1:" + env["AGENT_PORT"],
                                            trust_env=False, timeout=15,
                                            headers={"Authorization": "Bearer " + env["AGENT_ACCESS_TOKEN"]}))


def start_api(stack, root, env, directory, label, connection, *args):
    process = launch(stack, root, env, directory, label, *(args or ("run.py",)))

    def ready():
        try:
            return connection.get("/api/v1/agent/scenarios").is_success
        except httpx.TransportError:
            return False

    wait_until(ready, [process], "真实 API 启动")
    return process


def frames(lines):
    """严格消费完整 SSE 帧，保留服务端持久 ID 和原数据。"""
    frame = {}
    for line in lines:
        if not line:
            if frame:
                assert set(frame) == {"id", "event", "data"}, frame
                yield {"id": int(frame["id"]), "event": frame["event"], "data": json.loads(frame["data"])}
                frame = {}
        elif not line.startswith(":"):
            key, value = line.split(":", 1)
            assert key not in frame, line
            frame[key] = value.lstrip()
    assert not frame, "网络流在完整 SSE 帧之前结束"


def backup_state(source, destination):
    """调用者已停止全部写入者；两数据库都经 backup API 生成一致副本。"""
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns("*.db", "*.db-wal", "*.db-shm",
                                                                      "*.sqlite3", "*.sqlite3-wal", "*.sqlite3-shm",
                                                                      "*.log"))
    for relative in (Path("data.db"), Path("skills/skills.sqlite3")):
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(source / relative) as current, sqlite3.connect(target) as copied:
            current.backup(copied)
            assert copied.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert current.execute("SELECT name,sql FROM sqlite_master ORDER BY name").fetchall() == copied.execute(
                "SELECT name,sql FROM sqlite_master ORDER BY name").fetchall()
            for (name,) in current.execute("SELECT name FROM sqlite_master WHERE type='table'"):
                quoted = '"' + name.replace('"', '""') + '"'
                assert current.execute("SELECT * FROM " + quoted).fetchall() == copied.execute(
                    "SELECT * FROM " + quoted).fetchall()
