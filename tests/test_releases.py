import asyncio
import importlib.util
import json
import py_compile
import shutil
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.catalog import Catalog
from app.errors import Fault
from app.release_routes import install_release_routes
from app.releases import Releases
from app.snapshots import Snapshots
from app.store import Store

ROOT = Path(__file__).resolve().parents[1]
ADMIN = {"tenant_id": "local", "org_id": "ops", "user_id": "publisher"}
BASE = "/api/v1/agent-admin"


@pytest.fixture
def registry(tmp_path):
    live = tmp_path / "live"
    folder = shutil.copytree(ROOT / "scenarios" / "notes", live / "notes", ignore=shutil.ignore_patterns("__pycache__"))
    store = Store(tmp_path / "test.db")
    snapshots = Snapshots(tmp_path / "test.db", Catalog(live))
    reports = {"passed": True, "mode": "offline"}
    releases = Releases(store, snapshots, candidates=tmp_path / "candidates",
                        evaluate=lambda catalog, scene: dict(reports))
    def candidate(tag="next", version="1.1.0", change=None):
        target = shutil.copytree(folder, tmp_path / "candidates" / tag / "notes", ignore=shutil.ignore_patterns("__pycache__"))
        path = target / "scenario.json"
        manifest = json.loads(path.read_text(encoding="utf8"))
        manifest["version"] = version
        if change:
            change(manifest)
        path.write_text(json.dumps(manifest), encoding="utf8")
        return target

    def call(method, *args):
        return asyncio.run(getattr(releases, method)(*args, actor=ADMIN))
    yield releases, candidate, call, reports
    store.close()


def test_stable_canary_and_rollback_survive_second_connection(registry, tmp_path):
    releases, candidate, call, _ = registry
    assert releases.select("notes", ADMIN, "cohort") is None
    candidate()
    target = call("stage", "notes", "next")["release_id"]
    stable = releases.select("notes", ADMIN, "cohort")
    assert stable != target and stable == releases.snapshots.baseline("notes")
    call("activate", target, 50)
    another_store = Store(tmp_path / "test.db", recover_reactive=False)
    try:
        other = Releases(another_store, releases.snapshots)
        assignments = [releases.select("notes", ADMIN, str(i)) for i in range(100)]
        assert set(assignments) == {stable, target}
        assert assignments == [other.select("notes", ADMIN, str(i)) for i in range(100)]
        call("activate", target, 100)
        assert other.select("notes", ADMIN, "x") == target
        with pytest.raises(Fault, match="."):
            other.guard_selection(stable, "notes", ADMIN, "x")
        call("rollback", target)
        assert other.select("notes", ADMIN, "x") == stable
        assert other.current_tool("notes", ADMIN, "x", "notes_search")["permission"] == "notes.read"
        other.guard(target, "notes", "notes_search")
    finally:
        another_store.close()


@pytest.mark.parametrize("change", [
    lambda t: t.update(permission="notes.new"),
    lambda t: t["input_schema"]["properties"]["text"].update(enum=["allowed"]),
    lambda t: t["input_schema"]["properties"]["text"].update(default="new value"),
    lambda t: t["input_schema"]["properties"]["text"].update(type="integer"),
    lambda t: t["output_schema"]["required"].remove("matches"),
    lambda t: t["output_schema"]["properties"].pop("matches"),
])
def test_breaking_minor_cannot_stage(registry, change):
    releases, candidate, call, _ = registry
    def modify(m):
        m.pop("offline_tool")
        change(m["tools"][0])
    candidate(change=modify)
    with pytest.raises(Fault):
        call("stage", "notes", "next")
    assert releases.select("notes", ADMIN, "key") is None


def test_major_break_accepted_but_same_version_cannot_change(registry):
    _, candidate, call, _ = registry
    modify = lambda m: m["tools"][0].update(permission="notes.new")
    candidate("major", "2.0.0", modify)
    target = call("stage", "notes", "major")["release_id"]
    assert call("stage", "notes", "major")["release_id"] == target
    candidate("other", "2.0.0")
    with pytest.raises(Fault) as e:
        call("stage", "notes", "other")
    assert e.value.code == "RELEASE_VERSION_CONFLICT"


def test_revoke_cannot_be_bypassed_by_old_pins_rollback_or_activation(registry):
    releases, candidate, call, _ = registry
    candidate()
    target = call("stage", "notes", "next")["release_id"]
    stable = releases.select("notes", ADMIN, "x")
    call("activate", target, 100)
    releases.revoke(stable, actor=ADMIN)
    for action in (lambda: releases.guard(stable, "notes", "notes_search"),
                   lambda: call("rollback", target)):
        with pytest.raises(Fault) as e:
            action()
        assert e.value.code == "CAPABILITY_REVOKED"
    releases.revoke_capability("notes", "notes_search", actor=ADMIN)
    for digest in (None, stable, target):
        with pytest.raises(Fault):
            releases.guard(digest, "notes", "notes_search")
    with pytest.raises(Fault):
        call("activate", target, 100)


def test_management_gate_fails_closed_at_each_transition(registry):
    releases, candidate, call, reports = registry
    candidate()
    reports["passed"] = False
    for method in ("validate", "stage"):
        with pytest.raises(Fault) as e:
            call(method, "notes", "next")
        assert e.value.code == "EVALUATION_GATE_FAILED"
    reports["passed"] = True
    target = call("stage", "notes", "next")["release_id"]
    reports["passed"] = False
    with pytest.raises(Fault):
        call("activate", target, 100)
    reports["passed"] = True
    call("activate", target, 100)
    reports["passed"] = False
    with pytest.raises(Fault):
        call("rollback", target)
    assert releases.select("notes", ADMIN, "x") == target


def test_unmanaged_emergency_revoke_and_candidate_path_validation(registry):
    releases, _, call, _ = registry
    snapshot = releases.snapshots.baseline("notes")
    releases.revoke(snapshot, actor=ADMIN)
    with pytest.raises(Fault):
        releases.guard(None, "notes")
    with pytest.raises(Fault):
        call("stage", "notes", "../live")


def test_input_range_widening_and_optional_addition_remain_minor(registry):
    _, candidate, call, _ = registry
    def change(manifest):
        properties = manifest["tools"][0]["input_schema"]["properties"]
        properties["text"]["maxLength"] *= 2
        properties["optional"] = {"type": "boolean"}
    candidate(change=change)
    assert call("stage", "notes", "next")["breaking_changes"] == []


def test_candidate_may_be_restaged_after_rollback_but_revocation_is_final(registry):
    releases, candidate, call, _ = registry
    candidate()
    rid = call("stage", "notes", "next")["release_id"]
    call("activate", rid, 100)
    call("rollback", rid)
    call("stage", "notes", "next")
    assert call("activate", rid, 100)["status"] == "active"
    releases.revoke(rid, actor=ADMIN)
    evaluations = []
    releases.evaluate = lambda c, s: evaluations.append(s) or {"passed": True}
    with pytest.raises(Fault):
        call("stage", "notes", "next")
    assert evaluations == []


def test_successive_rollbacks_restore_each_prior_release(registry):
    releases, candidate, call, _ = registry
    original = releases.snapshots.baseline("notes")
    candidate("one", "1.1.0")
    first = call("stage", "notes", "one")["release_id"]
    call("activate", first, 100)
    candidate("two", "1.2.0")
    second = call("stage", "notes", "two")["release_id"]
    call("activate", second, 100)
    assert call("rollback", second)["release_id"] == first
    assert call("rollback", first)["release_id"] == original


def test_tampering_with_opened_snapshot_blocks_further_execution(registry):
    releases, candidate, call, _ = registry
    candidate()
    rid = call("stage", "notes", "next")["release_id"]
    file = releases.snapshots.directory / rid / "notes" / "adapter.py"
    file.write_text("# changed", encoding="utf-8")
    with pytest.raises(Fault) as error:
        releases.snapshots.open(rid, "notes")
    assert error.value.code == "RELEASE_SNAPSHOT_INVALID"


def test_rejected_candidate_never_imports_its_python_module(registry, tmp_path):
    releases, candidate, call, reports = registry
    folder = candidate()
    marker = tmp_path / "unexpected-import"
    file = folder / "adapter.py"
    file.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n" + file.read_text(encoding="utf8"),
                    encoding="utf8")
    reports["passed"] = False
    with pytest.raises(Fault):
        call("stage", "notes", "next")
    assert not marker.exists()


def test_invalid_candidate_data_filename_not_copied_to_snapshot(registry):
    releases, candidate, call, _ = registry
    folder = candidate(change=lambda m: m.update(documents=".env"))
    (folder / ".env").write_text("[]", encoding="utf8")
    with pytest.raises(Fault):
        call("stage", "notes", "next")
    assert not list(releases.snapshots.directory.rglob(".env"))


def test_unchecked_bytecode_cannot_substitute_for_digest_verified_source(registry, tmp_path):
    releases, candidate, call, _ = registry
    candidate()
    rid = call("stage", "notes", "next")["release_id"]
    folder = releases.snapshots.directory / rid / "notes"
    trusted = folder / "adapter.py"
    marker = tmp_path / "unexpected-bytecode"
    hostile = tmp_path / "different.py"
    hostile.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
                       + trusted.read_text(encoding="utf8"), encoding="utf8")
    py_compile.compile(str(hostile), cfile=importlib.util.cache_from_source(str(trusted)),
                       invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH, doraise=True)
    releases.snapshots.cache.clear()
    releases.snapshots.open(rid, "notes")
    assert not marker.exists()


def test_admin_client_cannot_assert_gate_pass_or_upload_path(registry):
    releases, candidate, _, _ = registry
    candidate()
    app = FastAPI()
    app.state.releases = releases
    from fastapi import Header, HTTPException
    def admin(authorization: str = Header("")):
        if authorization != "Bearer admin-test":
            raise HTTPException(403)
        return ADMIN
    install_release_routes(app, None, admin)
    with TestClient(app) as browser:
        url = BASE + "/releases"
        body = {"business_id": "notes", "candidate": "next"}
        assert browser.post(url, json=body).status_code == 403
        headers = {"Authorization": "Bearer admin-test"}
        assert browser.post(url, headers=headers, json=body | {"passed": True}).status_code == 422
        response = browser.post(url, headers=headers, json=body)
        assert response.is_success, response.text
        rid = response.json()["release_id"]
        assert browser.get(url + "/" + rid, headers=headers).json()["version"] == "1.1.0"
        assert browser.post(url + "/" + rid + "/activate", headers=headers, json={"canary_percent": 100}).is_success
