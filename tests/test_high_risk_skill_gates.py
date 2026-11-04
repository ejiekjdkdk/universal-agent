"""高风险审计：已发布 Skill 的运行时 Schema 需要有界执行。"""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from app.skills import SkillRegistry


@pytest.mark.parametrize("message", ["aaa", "a" * 40 + "!"])
def test_published_skill_input_validation_has_bounded_runtime(tmp_path, message):
    folder = tmp_path / "lookup"
    (folder / "schemas").mkdir(parents=True)
    (folder / "SKILL.md").write_text("---\nname: lookup\ndescription: 查询资料\n---\n保留来源。", encoding="utf-8")
    (folder / "skill.manifest.yaml").write_text("input_schema_ref: schemas/input.json\n", encoding="utf-8")
    (folder / "schemas/input.json").write_text(json.dumps({
        "type": "object", "properties": {"message": {"type": "string", "pattern": "^(a+)+$"}}
    }), encoding="utf-8")
    registry = SkillRegistry(tmp_path / "registry")
    registry.import_package(folder)
    registry.publish("demo", "lookup", "1.0.0")
    assert registry.governance("demo", "lookup", "1.0.0")["status"] == "active"
    code = '''
import sys
from app.runtime import Runtime
from app.skills import SkillRegistry
from app.errors import Fault
runtime = object.__new__(Runtime)
runtime.skills = SkillRegistry(sys.argv[1])
print("READY", flush=True)
try:
    runtime.validate_skill_input("demo", {"name": "lookup", "version": "1.0.0"}, sys.argv[2], {})
except Fault as error:
    assert error.code == "SKILL_SCHEMA_INVALID"
    print("DENIED", flush=True)
else:
    print("VALID", flush=True)
print("FINISHED", flush=True)
'''
    process = subprocess.Popen([sys.executable, "-c", code, str(tmp_path / "registry"), message],
                               cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    timed_out = False
    try:
        assert process.stdout.readline().strip() == "READY"
        try:
            output, errors = process.communicate(timeout=8)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            output, errors = process.communicate()
        assert not timed_out, "已通过真实发布门禁的 Schema 未在有界校验和回收时间内结束"
        assert process.returncode == 0, errors
        assert "FINISHED" in output
        if message == "aaa":
            assert "VALID" in output
        else:
            assert "DENIED" in output
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
