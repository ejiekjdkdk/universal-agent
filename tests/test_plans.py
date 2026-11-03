import asyncio
import json
import shutil
from pathlib import Path

import pytest

from app.catalog import Catalog
from app.errors import Fault
from app.plans import prepare_plan, resolve_arguments


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "durable" / "scenarios" / "project_brief"


@pytest.fixture
def scene(tmp_path):
    folder = shutil.copytree(EXAMPLE, tmp_path / "project_brief")
    return tmp_path, folder


def read(file):
    return json.loads(file.read_text(encoding="utf8"))


def write(file, value):
    file.write_text(json.dumps(value), encoding="utf8")


def build(scene, change=None, *, budget=None, permissions=("brief.read",), context=None):
    root, folder = scene
    file = folder / "plan.json"
    if change:
        template = read(file)
        change(template)
        write(file, template)
    return prepare_plan(Catalog(root), "project_brief", "任务", permissions, context or {}, budget=budget)


def test_diamond_plan_is_typed_and_resolves_without_reinterpreting_data(scene):
    plan, budget = build(scene)
    a, b, c = plan["steps"]
    assert plan["schema_version"] == "2.0" and plan["result_contract"] == "summary_v1"
    assert a["depends_on"] == b["depends_on"] == [] and c["depends_on"] == [a["step_id"], b["step_id"]]
    assert all(step["required"] for step in plan["steps"])
    assert a["arguments"]["text"] == "任务"
    assert budget.keys() == {"max_steps", "max_parallel_steps", "max_tool_calls", "deadline_ms"}
    inputs = resolve_arguments(c, {a["step_id"]: {"data": {"summary": "进度"}},
                                   b["step_id"]: {"data": {"summary": "风险"}}})
    assert inputs == {"progress": "进度", "risks": "风险"}
    with pytest.raises(Fault):
        resolve_arguments(c, {a["step_id"]: {"data": {}}})


def set_final_ref(plan, value):
    plan["steps"][-1]["arguments"]["progress"] = value


@pytest.mark.parametrize(("change", "code"), [
    (lambda p: p.update(task_id="forged"), "PLAN_INVALID"),
    (lambda p: p.update(result_contract="other"), "PLAN_INVALID"),
    (lambda p: p["steps"][0].update(required=1), "PLAN_INVALID"),
    (lambda p: p["steps"][0].update(adapter_ref="http://host"), "PLAN_INVALID"),
    (lambda p: p["steps"][0].update(step_id=p["steps"][1]["step_id"]), "PLAN_INVALID"),
    (lambda p: p["steps"][0].update(depends_on=["absent"]), "PLAN_INVALID"),
    (lambda p: p["steps"][0].update(depends_on=[p["steps"][-1]["step_id"]]), "PLAN_CYCLE_DETECTED"),
    (lambda p: p["steps"][0].update(capability_id="other_tool"), "PLAN_INVALID"),
    (lambda p: p["steps"][-1].update(depends_on=[]), "PLAN_INVALID"),
    (lambda p: set_final_ref(p, {"$step_output": "progress", "path": "/execution_id"}), "PLAN_INVALID"),
    (lambda p: set_final_ref(p, {"$step_output": "progress", "path": "/data/absent"}), "PLAN_INVALID"),
    (lambda p: set_final_ref(p, {"$step_output": "progress", "path": "/data/bad~2key"}), "PLAN_INVALID"),
    (lambda p: set_final_ref(p, {"$step_output": "progress", "path": "/data/count"}), "PLAN_INVALID"),
    (lambda p: set_final_ref(p, {"$message": 1}), "PLAN_INVALID"),
    (lambda p: p["steps"][0]["arguments"].update(text=4), "PLAN_INVALID"),
    (lambda p: p["steps"][0]["arguments"].update(evil=True), "PLAN_INVALID"),
])
def test_invalid_template_is_rejected_before_dispatch(scene, change, code):
    with pytest.raises(Fault) as exc:
        build(scene, change)
    assert exc.value.code == code


@pytest.mark.parametrize("budget", [{"max_steps": 2}, {"max_tool_calls": 2}, {"deadline_ms": 900001},
                                    {"max_parallel_steps": 4}, {"max_steps": True}, {"url": 2}])
def test_budget_is_capped_server_side(scene, budget):
    with pytest.raises(Fault) as exc:
        build(scene, budget=budget)
    assert exc.value.code == "EXECUTION_BUDGET_EXCEEDED"


def test_permissions_and_object_binding_are_independently_checked(scene):
    with pytest.raises(Fault) as exc:
        build(scene, permissions=[])
    assert exc.value.code == "PERMISSION_DENIED"
    file = scene[1] / "scenario.json"
    manifest = read(file)
    tool = manifest["tools"][0]
    tool["object_ids_field"] = "ids"
    tool["input_schema"]["properties"]["ids"] = {"type": "array", "items": {"type": "string"}}
    tool["input_schema"]["required"].append("ids")
    write(file, manifest)
    with pytest.raises(Fault) as exc:
        build(scene, lambda p: p["steps"][0]["arguments"].update(ids=["outside"]),
              context={"object_ids": ["allowed"]})
    assert exc.value.code == "PERMISSION_DENIED"


def test_missing_plan_does_not_break_v1_catalog(scene):
    (scene[1] / "plan.json").unlink()
    with pytest.raises(Fault) as exc:
        build(scene)
    assert exc.value.code == "PLAN_NOT_FOUND"


def test_read_only_and_depth_limit(scene):
    file = scene[1] / "scenario.json"
    manifest = read(file)
    manifest["tools"][0]["action_level"] = "propose_write"
    write(file, manifest)
    with pytest.raises(Fault) as exc:
        build(scene)
    assert exc.value.code == "ACTION_NOT_ALLOWED"
    manifest["tools"][0]["action_level"] = "read"
    write(file, manifest)
    def deep(plan):
        base = plan["steps"][0]
        plan["steps"] = [dict(base, step_id=f"s{i}", depends_on=[f"s{i-1}"] if i else [])
                         for i in range(7)]
    with pytest.raises(Fault) as exc:
        build(scene, deep)
    assert exc.value.code == "EXECUTION_BUDGET_EXCEEDED"


def test_example_retrieves_both_sources_and_builds_brief(scene):
    plan, _ = build(scene)
    catalog = Catalog(scene[0])
    async def execute(plan):
        outputs = {}
        for step in plan["steps"]:
            output = await catalog.execute("project_brief", step["capability_id"], resolve_arguments(step, outputs))
            outputs[step["step_id"]] = output
        return outputs

    outputs = asyncio.run(execute(plan))
    assert outputs["progress"]["evidence"][0]["source_id"] == "brief-progress"
    assert outputs["risks"]["evidence"][0]["source_id"] == "brief-risks"
    for branch in ("progress", "risks"):
        assert outputs[branch]["evidence"][0]["snippet"] in outputs["brief"]["data"]["summary"]


def test_transitive_pointer_into_array_and_escaped_property_name(scene):
    file = scene[1] / "scenario.json"
    manifest = read(file)
    manifest["tools"][0]["output_schema"]["properties"]["a/b~c"] = {
        "type": "array", "items": {"type": "string"}, "maxItems": 2}
    write(file, manifest)
    def change(plan):
        a, b, c = plan["steps"]
        b["depends_on"] = ["progress"]
        c["depends_on"] = ["risks"]
        c["arguments"]["progress"]["path"] = "/data/a~1b~0c/0"
        plan["steps"] = [c, b, a]
    plan, _ = build(scene, change)
    assert [s["step_id"] for s in plan["steps"]] == ["progress", "risks", "brief"]
    args = resolve_arguments(plan["steps"][-1], {"progress": {"data": {"a/b~c": ["ok"]}},
                                                "risks": {"data": {"summary": "none"}}})
    assert args == {"progress": "ok", "risks": "none"}


def test_non_finite_literal_is_rejected_as_structured_plan_error(scene):
    file = scene[1] / "scenario.json"
    manifest = read(file)
    manifest["tools"][0]["input_schema"]["properties"]["x"] = {"type": "number"}
    write(file, manifest)
    file = scene[1] / "plan.json"
    raw = file.read_text(encoding="utf8").replace('"text":', '"x": 1e999, "text":', 1)
    file.write_text(raw, encoding="utf8")
    with pytest.raises(Fault) as exc:
        build(scene)
    assert exc.value.code == "PLAN_INVALID"
