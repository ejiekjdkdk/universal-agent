"""技能绑定随持久计划保存，审批与恢复仍由同一执行器管理。"""
from pathlib import Path
import json
import time

from fastapi.testclient import TestClient
import pytest

from app.main import API, create_app
from app.errors import Fault


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("field", ["input", "output"])
def test_missing_local_schema_reference_is_a_structured_error(tmp_path, field):
    app = create_app(scenarios=ROOT / "scenarios", database=tmp_path / "db", token="reader-test")
    with TestClient(app, base_url="http://localhost", headers={"Authorization": "Bearer reader-test"}) as client:
        runtime = app.state.runtime
        folder = tmp_path / "missing-ref"
        (folder / "schemas").mkdir(parents=True)
        (folder / "SKILL.md").write_text(
            "---\nname: missing-ref\ndescription: 引用错误分类\n---\n按来源回答。", encoding="utf-8")
        (folder / "skill.manifest.yaml").write_text(
            f"{field}_schema_ref: schemas/value.json\n", encoding="utf-8")
        (folder / "schemas/value.json").write_text('{"$ref":"#/missing"}', encoding="utf-8")
        runtime.skills.import_package(folder)
        tools = ["notes_search", "retrieve_knowledge"]
        runtime.skills.publish("notes", "missing-ref", "1.0.0", tools, available_tools=tools)
        response = client.post(API + "/chat", json={"business_id": "notes", "message": "会议纪要", "mode": "mock"})
        result = response.json()
        assert result["error"]["code"] == "SKILL_SCHEMA_INVALID", result
        if field == "input":
            assert response.status_code == 422
        else:
            assert result["status"] == "failed" and result["answer"] == ""


def test_durable_skill_pin_and_otlp_link(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_ADMIN_TOKEN", "administrator-for-this-test")
    app = create_app(scenarios=ROOT / "examples/durable/scenarios", database=tmp_path / "db", token="reader-test")
    with TestClient(app, base_url="http://localhost", headers={"Authorization": "Bearer reader-test"}) as client:
        rt = app.state.runtime
        package = tmp_path / "project-summary"
        package.mkdir()
        (package / "SKILL.md").write_text("---\nname: project-summary\ndescription: 本地资料整理\n---\n汇总授权资料。", encoding="utf-8")
        rt.skills.import_package(package)
        tools = [tool["name"] for tool in rt.catalog.discover("project_brief")]
        tools.append("retrieve_knowledge")
        rt.skills.publish("project_brief", "project-summary", "1.0.0", tools, available_tools=tools)
        body = {"business_id": "project_brief", "message": "整理项目资料",
                "skill_id": "project-summary", "execution_mode": "durable_plan"}
        accepted = client.post(API + "/chat", json=body)
        assert accepted.status_code == 202, accepted.text
        tid = accepted.json()["task_id"]
        deadline = time.monotonic() + 10
        task = accepted.json()
        while task["status"] == "running" and time.monotonic() < deadline:
            time.sleep(.05)
            task = client.get(API + "/tasks/" + tid).json()
        assert task["status"] == "completed", task
        pin = rt.store.skill_pin(tid)
        assert pin["name"] == "project-summary"
        trace = client.get("/api/v1/agent-admin/observability/traces/" + tid,
                           headers={"Authorization": "Bearer administrator-for-this-test"}).json()
        assert pin["release_snapshot_id"] in str(trace)


def test_frozen_skill_output_rejected_before_answer_event(tmp_path):
    app = create_app(scenarios=ROOT / "scenarios", database=tmp_path / "db", token="reader-test")
    with TestClient(app, base_url="http://localhost", headers={"Authorization": "Bearer reader-test"}) as client:
        rt = app.state.runtime
        folder = tmp_path / "checked"
        (folder / "schemas").mkdir(parents=True)
        (folder / "SKILL.md").write_text("---\nname: checked\ndescription: 校验结果\n---\n按来源回答。", encoding="utf-8")
        (folder / "skill.manifest.yaml").write_text("output_schema_ref: schemas/output.json\n", encoding="utf-8")
        (folder / "schemas/output.json").write_text(json.dumps(
            {"type": "object", "properties": {"answer": {"const": "schema-required-value"}}, "required": ["answer"]}
        ), encoding="utf-8")
        rt.skills.import_package(folder)
        tools = ["notes_search", "retrieve_knowledge"]
        rt.skills.publish("notes", "checked", "1.0.0", tools, available_tools=tools)
        task = client.post(API + "/chat", json={"business_id": "notes", "message": "会议纪要", "mode": "mock"}).json()
        assert task["error"]["code"] == "SKILL_SCHEMA_INVALID" and task["answer"] == "", task
        events = client.get(API + "/tasks/" + task["task_id"] + "/trace").json()["events"]
        assert "message_delta" not in [entry["event"] for entry in events]


def test_skill_physical_budget_includes_retry(tmp_path, monkeypatch):
    app = create_app(scenarios=ROOT / "scenarios", database=tmp_path / "db", token="reader-test")
    with TestClient(app, base_url="http://localhost", headers={"Authorization": "Bearer reader-test"}) as client:
        rt = app.state.runtime
        folder = tmp_path / "bounded"
        folder.mkdir()
        (folder / "SKILL.md").write_text("---\nname: bounded\ndescription: 限制实际请求\n---\n按来源回答。", encoding="utf-8")
        (folder / "skill.manifest.yaml").write_text("execution:\n  max_tool_calls: 1\n", encoding="utf-8")
        rt.skills.import_package(folder)
        tools = ["notes_search", "retrieve_knowledge"]
        rt.skills.publish("notes", "bounded", "1.0.0", tools, available_tools=tools)
        attempts = []

        async def unavailable(*args, **kwargs):
            attempts.append(1)
            raise Fault("UPSTREAM_UNAVAILABLE", "模拟暂时不可用", 503, True)

        monkeypatch.setattr("app.catalog.Catalog.execute", unavailable)
        task = client.post(API + "/chat", json={"business_id": "notes", "message": "会议纪要", "mode": "mock"}).json()
        assert task["error"]["code"] == "BUDGET_EXCEEDED", task
        assert len(attempts) == 1
