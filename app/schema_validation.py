"""在可终止的独立进程中校验外部 Schema，防止正则阻塞服务进程。"""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import threading


def is_valid(schema, value, *, dialect="2020-12"):
    """只返回校验结论；超时、解析失败和子进程异常均拒绝，不回显输入。"""
    try:
        payload = json.dumps([schema, value, dialect], ensure_ascii=True, allow_nan=False).encode("utf-8")
        if len(payload) > 8 * 1024 * 1024:
            return False
        if len(payload) <= 65536 and _simple_schema(schema):
            # 小型、无正则/引用/组合规则的Schema保留快速路径，避免进程启动耗尽工具时限。
            from jsonschema import Draft202012Validator
            return Draft202012Validator(schema).is_valid(value)
        # 子进程只接收本次校验数据，不继承服务令牌、模型或MCP凭据。
        environment = {}
        for name in ("SystemRoot", "WINDIR"):
            if name in os.environ:
                environment[name] = os.environ[name]
        flags = 0
        if os.name == "nt":
            flags = subprocess.CREATE_NO_WINDOW
        with subprocess.Popen(
            [sys.executable, "-I", str(Path(__file__).resolve())],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=environment,
            creationflags=flags,
        ) as process:
            started = threading.Event()
            ready: list[bytes] = []
            assert process.stdout is not None
            output_stream = process.stdout

            def read_ready():
                # 初始化尚未接收不可信输入；冷启动不能占用实际规则校验预算。
                try:
                    ready.append(output_stream.readline())
                finally:
                    started.set()

            reader = threading.Thread(target=read_ready, daemon=True)
            reader.start()
            try:
                if not started.wait(5) or ready != [b"ready\n"]:
                    return False
                reader.join()
                output, _ = process.communicate(input=payload, timeout=2)
                return process.returncode == 0 and output == b"valid"
            finally:
                if process.poll() is None:
                    process.kill()
                # 先让就绪读取退出，再排空管道；不能同时有两个线程读取stdout。
                reader.join()
                process.communicate()
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError, RecursionError):
        return False


def _simple_schema(schema):
    """仅放行有限树形结构的基础规则；未知或可重复展开的规则一律隔离。"""
    allowed = {
        "type", "properties", "required", "additionalProperties", "items",
        "minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems",
        "title", "description", "default",
    }
    pending = [(schema, 0)]
    count = 0
    while pending:
        current, depth = pending.pop()
        count += 1
        if count > 128 or depth > 8:
            return False
        if isinstance(current, bool):
            continue
        if not isinstance(current, dict) or set(current) - allowed:
            return False
        properties = current.get("properties", {})
        if not isinstance(properties, dict) or len(properties) > 128:
            return False
        for child in properties.values():
            pending.append((child, depth + 1))
        for name in ("items", "additionalProperties"):
            if name in current:
                pending.append((current[name], depth + 1))
    return True


async def is_valid_async(schema, value, *, dialect="2020-12"):
    """异步调用不占用事件循环；取消时等待有硬超时的子进程回收。"""
    worker = asyncio.create_task(asyncio.to_thread(is_valid, schema, value, dialect=dialect))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        await worker
        raise


def _validate_input():
    """固定入口不执行技能代码；禁止网络引用，结果通道不包含原始数据。"""
    from jsonschema import Draft202012Validator
    from jsonschema.validators import validator_for
    from referencing import Registry
    from referencing.exceptions import NoSuchResource

    sys.stdout.buffer.write(b"ready\n")
    sys.stdout.buffer.flush()

    def deny_remote(uri):
        raise NoSuchResource(ref=uri)

    try:
        raw = sys.stdin.buffer.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            return
        schema, value, dialect = json.loads(raw)
        validator_type = Draft202012Validator
        if dialect == "auto":
            validator_type = validator_for(schema)
            validator_type.check_schema(schema)
        validator = validator_type(schema, registry=Registry(retrieve=deny_remote))
        if validator.is_valid(value):
            sys.stdout.buffer.write(b"valid")
    except Exception:
        # 隔离进程把所有不可信校验错误转为拒绝，避免异常携带输入流入日志。
        return


if __name__ == "__main__":
    _validate_input()
