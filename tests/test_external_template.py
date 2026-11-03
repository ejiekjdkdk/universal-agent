"""Exercise the copied starter using separate project and agent processes."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def test_external_project_uses_live_data_and_public_trace(tmp_path):
    project = tmp_path / "separate project"
    shutil.copytree(ROOT / "templates" / "external-project", project,
                    ignore=shutil.ignore_patterns("__pycache__", ".venv", "var"))
    records = project / "records.json"
    data = json.loads(records.read_text(encoding="utf-8"))
    data["DOC-101"]["status"] = "verified-in-another-project"
    records.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "demo.py", "--agent-root", str(ROOT)],
        cwd=project, capture_output=True, encoding="utf-8", timeout=75,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert "verified-in-another-project" in report["answer"]
    assert report["source_id"] == "DOC-101"
    assert report["tool"] == "lookup_record"
    assert report["checks"]["project_api_calls"] == 1
    assert report["checks"]["upstream_offline"] == "failed"
    assert report["checks"]["idempotent_replay"] is True
    assert report["checks"]["unauthorized_record_read"] == 401
