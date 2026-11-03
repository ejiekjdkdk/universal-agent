"""旧场景的资料内容按公开 API、快照与发布路径分别验收。"""
import json
from pathlib import Path
import shutil

from fastapi.testclient import TestClient
import pytest

from app.catalog import Catalog
from app.main import create_app
from app.snapshots import Snapshots

ROOT = Path(__file__).resolve().parents[1]


def notes_copy(tmp_path, name):
    scene = shutil.copytree(ROOT / "scenarios" / "notes", tmp_path / "scenarios" / "notes")
    manifest = scene / "scenario.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    (scene / data["documents"]).rename(scene / name)
    data["documents"] = name
    manifest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return scene


def test_legacy_notes_txt_starts_and_serves_reactive_queries(tmp_path):
    scene = notes_copy(tmp_path, "会议笔记.txt")
    db = tmp_path / "legacy.db"
    catalog = Catalog(scene.parent)
    expected = catalog.retrieve("notes", "会议纪要")
    assert expected
    with TestClient(create_app(scenarios=scene.parent, database=db, token="notes-test", model_settings={}),
                    base_url="http://localhost") as client:
        headers = {"Authorization": "Bearer notes-test"}
        response = client.post("/api/v1/agent/chat", headers=headers,
                               json={"business_id":"notes", "message":"会议纪要"})
        assert response.status_code == 200
        assert response.json()["status"] == "completed"
        assert response.json()["evidence"] == expected
        frozen = client.app.state.snapshots
        catalog_from_disk = frozen.open(frozen.baseline("notes"), "notes")
        assert catalog_from_disk.retrieve("notes", "会议纪要") == expected


@pytest.mark.parametrize("name", [".env", "../secret.txt", "a:b.txt", "plan.json"])
def test_candidate_document_path_is_rejected_before_copy(tmp_path, name):
    live = notes_copy(tmp_path, "records.json")
    snapshots = Snapshots(tmp_path / "db", Catalog(live.parent))
    candidate = shutil.copytree(live, tmp_path / "candidate" / "notes")
    manifest = candidate / "scenario.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["documents"] = name
    manifest.write_text(json.dumps(data), encoding="utf-8")
    if name in {".env", "plan.json", "../secret.txt"}:
        (candidate / name).write_text("[]", encoding="utf-8")
    directories = set(snapshots.directory.iterdir())
    with pytest.raises((ValueError, OSError)):
        snapshots.capture(candidate, "notes")
    assert set(snapshots.directory.iterdir()) == directories


def test_candidate_with_invalid_legacy_document_content_creates_no_snapshot(tmp_path):
    live = notes_copy(tmp_path, "records.json")
    snapshots = Snapshots(tmp_path / "db", Catalog(live.parent))
    candidate = shutil.copytree(live, tmp_path / "candidate" / "notes")
    manifest = candidate / "scenario.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["documents"] = "records.txt"
    manifest.write_text(json.dumps(data), encoding="utf-8")
    (candidate / "records.txt").write_text('{"private": "must stay outside the snapshot"}', encoding="utf-8")
    directories = set(snapshots.directory.iterdir())
    with pytest.raises(ValueError):
        snapshots.capture(candidate, "notes")
    assert set(snapshots.directory.iterdir()) == directories
