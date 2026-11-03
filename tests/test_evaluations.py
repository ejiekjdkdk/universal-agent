import json
import shutil
import asyncio
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.evaluations import evaluate_release


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(("root", "scene"), [
    ("scenarios", "notes"), ("scenarios", "text"), ("examples/durable/scenarios", "project_brief"),
    ("examples/governance/scenarios", "change_requests"),
])
def test_versioned_golden_replay(root, scene):
    source = SimpleNamespace(root=ROOT / root)
    first = evaluate_release(source, scene)
    assert first["passed"], first
    assert evaluate_release(source, scene) == first
    assert first["environment"] == "offline" and first["golden_version"]
    assert first["metrics"]["routing_accuracy"] == 1
    assert first["metrics"]["security_pass_rate"] == 1
    assert first["failed_cases"] == []


def test_candidate_cannot_replace_or_disable_fixed_goldens(tmp_path):
    folder = shutil.copytree(ROOT / "scenarios/notes", tmp_path / "notes")
    (folder / "evals.json").write_text('{"cases":[]}')
    notes = json.loads((folder / "notes.json").read_text(encoding="utf8"))
    notes[0]["source_id"] = "changed"
    notes[0]["snippet"] = "private-customer-material"
    (folder / "notes.json").write_text(json.dumps(notes), encoding="utf8")
    report = evaluate_release(SimpleNamespace(root=tmp_path), "notes")
    assert not report["passed"]
    assert any(c["case_id"] == "ee_notes_matching" for c in report["failed_cases"])
    assert "private-customer-material" not in json.dumps(report)


def test_gate_refuses_unknown_code_before_module_import(tmp_path):
    folder = shutil.copytree(ROOT / "scenarios/text", tmp_path / "text")
    marker = tmp_path / "side_effect"
    (folder / "adapter.py").write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()")
    report = evaluate_release(SimpleNamespace(root=tmp_path), "text")
    assert not report["passed"] and not marker.exists()


def test_empty_trusted_suite_blocks_release(tmp_path, monkeypatch):
    from app import evaluations
    suite = json.loads((evaluations.SUITES / "notes.json").read_text(encoding="utf8"))
    suite["cases"] = []
    (tmp_path / "notes.json").write_text(json.dumps(suite), encoding="utf8")
    monkeypatch.setattr(evaluations, "SUITES", tmp_path)
    report = evaluate_release(SimpleNamespace(root=ROOT / "scenarios"), "notes")
    assert not report["passed"] and report["metrics"]["routing_accuracy"] is None
    assert report["failed_cases"][0]["case_id"] == "fixed_suite_and_bundle"


def test_http_project_can_stage_and_activate_with_reviewed_recordings(tmp_path, monkeypatch):
    import httpx
    from app import evaluations
    from app.catalog import Catalog
    from app.releases import Releases
    from app.snapshots import Snapshots
    from app.store import Store

    sid, target = "remote_notes", "https://records.example/v1/query"
    live = tmp_path / "live"
    folder = live / sid
    folder.mkdir(parents=True)
    manifest = json.loads((ROOT / "scenarios/notes/scenario.json").read_text(encoding="utf8"))
    manifest.update(id=sid)
    del manifest["documents"]
    tool = manifest["tools"][0]
    tool["adapter"] = {"type": "http", "method": "POST", "url": target}
    (folder / "scenario.json").write_text(json.dumps(manifest))
    golden = json.loads((evaluations.SUITES / "notes.json").read_text(encoding="utf8"))
    golden.update(adapter_sha256=None, adapters={"notes_search": ["http", None, "notes.read", "read"]})
    golden["cases"] = golden["cases"][:1]
    case = golden["cases"][0]
    case.update(business_id=sid, expected_evidence_refs=[], answer_contains=["fixture result"])
    recording = {"capability_id": "notes_search", "method": "POST", "url": target, "arguments": {"text": case["input"]},
                 "status_code": 200, "response": {"data": {"summary": "fixture result", "matches": 0}, "evidence": []}}
    golden["http_fixtures"] = [recording]
    suites = tmp_path / "goldens"
    suites.mkdir()
    monkeypatch.setattr(evaluations, "SUITES", suites)
    file = suites / (sid + ".json")
    file.write_text(json.dumps(golden))
    def no_network(*args, **kwargs):
        pytest.fail("offline evaluation reached a network transport")
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_network)
    manifest["version"] = "1.1.0"
    candidate = tmp_path / "candidates" / "next" / sid
    candidate.mkdir(parents=True)
    (candidate / "scenario.json").write_text(json.dumps(manifest))
    with closing(Store(tmp_path / "db")) as store:
        releases = Releases(store, Snapshots(tmp_path / "db", Catalog(live)), candidates=tmp_path / "candidates",
                            evaluate=evaluate_release)
        actor = {"tenant_id": "local", "org_id": "ops", "user_id": "admin"}
        staged = asyncio.run(releases.stage(sid, "next", actor=actor))
        asyncio.run(releases.activate(staged["release_id"], 100, actor=actor))
        assert releases.select(sid, actor, "") == staged["release_id"]
    for change in ({"arguments": {"text": "different"}}, {"url": "https://other.example"}, {"method": "GET"}):
        golden["http_fixtures"] = [recording | change]
        file.write_text(json.dumps(golden))
        assert not evaluate_release(SimpleNamespace(root=live), sid)["passed"]
    golden["http_fixtures"] = []
    file.write_text(json.dumps(golden))
    assert not evaluate_release(SimpleNamespace(root=live), sid)["passed"]


def test_injected_document_cannot_authorize_write_even_if_gateway_regresses(monkeypatch):
    from app.runtime import Gateway
    original = Gateway.execute
    async def broken(self, name, args, call_id):
        if name == "forbidden_write":
            return {"data": {"summary": "unauthorized"}, "evidence": []}
        return await original(self, name, args, call_id)
    monkeypatch.setattr(Gateway, "execute", broken)
    report = evaluate_release(SimpleNamespace(root=ROOT / "scenarios"), "text")
    assert not report["passed"]
    assert any(c["case_id"] == "security_injected_document" for c in report["failed_cases"])
