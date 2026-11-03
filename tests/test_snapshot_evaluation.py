import asyncio
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from app import evaluations
from app.catalog import Catalog
from app.errors import Fault
from app.releases import Releases
from app.snapshots import Snapshots
from app.store import Store


ROOT = Path(__file__).resolve().parents[1]
ADMIN = {"tenant_id": "local", "org_id": "ops", "user_id": "publisher"}
# 审核过的测试适配器：允许无附属文件时读缺省值，以便检出漏复制文件造成的误放。
SOURCE = b"""from pathlib import Path

def search(arguments, retrieve):
    root = Path(__file__).parent
    sidecar = root / "extra.txt"
    value = sidecar.read_text(encoding="utf-8") if sidecar.is_file() else "default"
    if (root / "not_published.txt").exists():
        value = "undeclared"
    source_kind = "crlf" if b"\\r\\n" in (root / "adapter.py").read_bytes() else "lf"
    return {"data": {"summary": value + "/" + source_kind, "matches": 0}, "evidence": []}
"""


@pytest.fixture
def project(tmp_path, monkeypatch):
    live = tmp_path / "live"
    scene = shutil.copytree(ROOT / "scenarios" / "notes", live / "notes",
                            ignore=shutil.ignore_patterns("__pycache__"))
    (scene / "adapter.py").write_bytes(SOURCE.replace(b"\n", b"\r\n"))
    manifest = json.loads((scene / "scenario.json").read_bytes())
    manifest.update(snapshot_files=["extra.txt"])
    (scene / "scenario.json").write_text(json.dumps(manifest), encoding="utf-8")
    (scene / "extra.txt").write_bytes(b"published")
    # 不属于快照的邻接文件即使存在，也不能被隔离评测或实际快照中的适配器看到。
    (scene / "not_published.txt").touch()
    candidate = shutil.copytree(scene, tmp_path / "candidates" / "next" / "notes")
    manifest["version"] = "1.1.0"
    (candidate / "scenario.json").write_text(json.dumps(manifest), encoding="utf-8")

    suite = json.loads((evaluations.SUITES / "notes.json").read_bytes())
    suite["adapter_sha256"] = hashlib.sha256(SOURCE).hexdigest()
    suite.pop("previous_adapter_sha256", None)
    suite["cases"] = [dict(suite["cases"][0], expected_evidence_refs=[], answer_contains=["published/crlf"])]
    suites = tmp_path / "goldens"
    suites.mkdir()
    golden = suites / "notes.json"
    golden.write_text(json.dumps(suite), encoding="utf-8")
    monkeypatch.setattr(evaluations, "SUITES", suites)
    return SimpleNamespace(scene=scene, candidate=candidate, golden=golden, database=tmp_path / "db")


def setup_release(project, store):
    snapshots = Snapshots(project.database, Catalog(project.scene.parent))
    return Releases(store, snapshots, candidates=project.candidate.parents[1], evaluate=evaluations.evaluate_release)


def test_staged_snapshot_and_offline_gate_use_identical_declared_bytes(project):
    with closing(Store(project.database)) as store:
        releases = setup_release(project, store)
        result = asyncio.run(releases.stage("notes", "next", actor=ADMIN))
        assert result["evaluation"]["passed"]
        actual = releases.snapshots.open(result["release_id"], "notes")
        output = asyncio.run(actual.execute("notes", "notes_search", {"text": "probe"}))
        assert output["data"]["summary"] == "published/crlf"
        assert (actual.root / "notes" / "adapter.py").read_bytes() == (project.candidate / "adapter.py").read_bytes()


def test_gate_rejects_candidate_if_declared_sidecar_changes_answer(project):
    # 原门禁漏掉 extra.txt，导致它恰好返回缺省值并通过。此断言与源码的换行形式无关。
    suite = json.loads(project.golden.read_bytes())
    suite["cases"][0]["answer_contains"] = ["default/"]
    project.golden.write_text(json.dumps(suite), encoding="utf-8")
    with closing(Store(project.database)) as store:
        releases = setup_release(project, store)
        with pytest.raises(Fault) as error:
            asyncio.run(releases.stage("notes", "next", actor=ADMIN))
        assert error.value.code == "EVALUATION_GATE_FAILED"
        assert releases.select("notes", ADMIN, "") is None


def test_stage_cannot_publish_snapshot_tampered_with_during_passing_gate(project):
    with closing(Store(project.database)) as store:
        releases = setup_release(project, store)
        marker = project.database.parent / "unexpected-import"
        evaluations_seen = []

        def gate_while_files_change(bundle, scene_id):
            folder = bundle.root / scene_id
            (folder / "extra.txt").write_text("changed", encoding="utf-8")
            (folder / "adapter.py").write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()", encoding="utf-8")
            report = evaluations.evaluate_release(bundle, scene_id)
            evaluations_seen.append(report["passed"])
            return report

        releases.evaluate = gate_while_files_change
        with pytest.raises(Fault) as error:
            asyncio.run(releases.stage("notes", "next", actor=ADMIN))
        assert error.value.code == "RELEASE_SNAPSHOT_INVALID"
        assert evaluations_seen == [True]  # 评测操作只读取核验时冻结的字节。
        assert releases.select("notes", ADMIN, "") is None
        assert not marker.exists()


@pytest.mark.parametrize("bad_name", ["missing.txt", "../outside.txt", ".env", "adapter.py", "too_large.txt"])
def test_cli_gate_rejects_bad_declared_files(project, bad_name):
    manifest = json.loads((project.candidate / "scenario.json").read_bytes())
    manifest["snapshot_files"].append(bad_name)
    (project.candidate / "scenario.json").write_text(json.dumps(manifest), encoding="utf-8")
    if bad_name == "too_large.txt":
        (project.candidate / bad_name).write_bytes(b"x" * 2_000_001)
    report = evaluations.evaluate_release(SimpleNamespace(root=project.candidate.parent), "notes")
    assert not report["passed"]
    assert report["failed_cases"][0]["case_id"] == "fixed_suite_and_bundle"


def test_cli_gate_accepts_same_files_with_legacy_json_content_filename(project):
    manifest = json.loads((project.candidate / "scenario.json").read_bytes())
    (project.candidate / manifest["documents"]).rename(project.candidate / "笔记.txt")
    manifest["documents"] = "笔记.txt"
    (project.candidate / "scenario.json").write_text(json.dumps(manifest), encoding="utf-8")
    source = SimpleNamespace(root=project.candidate.parent)
    result = evaluations.evaluate_release(source, "notes")
    assert result["passed"], result["failed_cases"]


def test_cli_uses_bytes_already_read_if_source_directory_changes(project, monkeypatch):
    read_files = evaluations.snapshot_files
    marker = project.scene.parent / "unexpected-import"

    def read_then_replace(folder, *args, **kwargs):
        files = read_files(folder, *args, **kwargs)
        (folder / "extra.txt").write_bytes(b"replaced")
        (folder / "adapter.py").write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()", encoding="utf-8")
        return files

    monkeypatch.setattr(evaluations, "snapshot_files", read_then_replace)
    report = evaluations.evaluate_release(SimpleNamespace(root=project.candidate.parent), "notes")
    assert report["passed"], report["failed_cases"]
    assert not marker.exists()
