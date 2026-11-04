"""隔离校验须保留有效合同，并在取消或超时后回收工作进程。"""
import asyncio
import subprocess
import threading

import pytest

from app.schema_validation import is_valid, is_valid_async


@pytest.mark.parametrize("value, expected", [("abc", True), ("123", False)])
def test_local_reference_and_normal_pattern_remain_supported(value, expected):
    schema = {
        "$defs": {"name": {"type": "string", "pattern": "^[a-z]+$"}},
        "$ref": "#/$defs/name",
    }
    assert is_valid(schema, value) is expected


def test_missing_reference_and_external_reference_fail_closed():
    assert not is_valid({"$ref": "#/$defs/missing"}, "sample")
    assert not is_valid({"$ref": "file:///not-an-approved-schema.json"}, "sample")


def test_declared_output_dialect_preserves_sdk_semantics():
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "array", "items": [{"type": "string"}, {"type": "integer"}],
    }
    assert is_valid(schema, ["sample", 1], dialect="auto")
    assert not is_valid(schema, [1, "sample"], dialect="auto")


def test_slow_interpreter_start_does_not_consume_validation_budget(monkeypatch):
    original = subprocess.Popen
    children = []

    def start(command, **kwargs):
        # 模拟本机冷启动延迟，仍运行真实校验入口和普通正则。
        wrapper = "import runpy,sys,time; time.sleep(2.2); runpy.run_path(sys.argv[1], run_name='__main__')"
        delayed = [command[0], "-I", "-c", wrapper, command[-1]]
        kwargs["stderr"] = subprocess.PIPE
        child = original(delayed, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", start)
    valid = is_valid({"type": "string", "pattern": "^[a-z]+$"}, "abc")
    assert valid, [(child.returncode, child.communicate()[1]) for child in children]


def test_cancelled_validation_reaps_process_and_does_not_inherit_credentials(monkeypatch):
    monkeypatch.setenv("AGENT_SERVICE_TOKEN", "fictional-isolation-secret")
    original = subprocess.Popen
    started = threading.Event()
    children = []
    environments = []

    def start(*args, **kwargs):
        child = original(*args, **kwargs)
        children.append(child)
        environments.append(kwargs["env"])
        started.set()
        return child

    monkeypatch.setattr(subprocess, "Popen", start)

    async def run():
        validation = asyncio.create_task(is_valid_async({"type": "string", "pattern": "^(a+)+$"}, "a" * 40 + "!"))
        assert await asyncio.to_thread(started.wait, 5)
        validation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await validation
        assert len(children) == 1 and children[0].poll() is not None
        assert "AGENT_SERVICE_TOKEN" not in environments[0]

    asyncio.run(run())
