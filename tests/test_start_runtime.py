"""验证默认 Windows 入口拒绝未登记、与验收记录不一致的运行环境。"""
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("field", ["python", "version", "extension_sha256", "library_sha256",
                                   "source_id", "lock_sha256", "status"])
def test_startup_rejects_runtime_drift(field, monkeypatch):
    from scripts.start_runtime import EXPECTED_LOCK, EXPECTED_SQLITE, verify

    monkeypatch.setattr(sys, "prefix", "isolated-environment")
    report = {"python": "3.11.18", "sqlite": EXPECTED_SQLITE.copy(),
              "lock_sha256": EXPECTED_LOCK, "status": "PASS", "problems": []}
    verify(report)
    source = report["sqlite"] if field in report["sqlite"] else report
    source[field] = "different"
    with pytest.raises(ValueError):
        verify(report)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows launcher")
def test_cmd_checks_registration_and_lock_before_app(tmp_path):
    root = tmp_path / "app & demo"
    for name in ("start.cmd", "scripts/start_runtime.ps1", "scripts/start_runtime.py",
                 "scripts/release_check.py", "app/runtime_environment.py", "app/__init__.py",
                 "requirements-win-py311.lock"):
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, target)

    def invoke(*args):
        return subprocess.run(args, cwd=root, capture_output=True, timeout=45)

    cmd = ["cmd.exe", "/d", "/c", "start.cmd", "--check"]
    missing = invoke(*cmd)
    assert missing.returncode and b"--register" in missing.stderr
    pointer = root / ".runtime-python"
    for invalid in (r"C:Scripts\python.exe", r"\\offline\env\python.exe", r".\python.exe"):
        pointer.write_text(invalid, encoding="utf-8")
        rejected = invoke(*cmd)
        assert rejected.returncode and b"--register" in rejected.stderr
    registered = invoke(sys.executable, "-I", "scripts/start_runtime.py", "--register")
    assert registered.returncode == 0, registered.stderr
    assert invoke(*cmd).returncode == 0
    (root / "run.py").write_text(
        "import sys\nfrom pathlib import Path\n"
        "Path('launched').write_text(sys.executable, encoding='utf-8')\n", encoding="utf-8")
    assert invoke(*cmd[:-1]).returncode == 0
    marker = root / "launched"
    assert Path(marker.read_text(encoding="utf-8")).samefile(sys.executable)
    marker.unlink()
    with (root / "requirements-win-py311.lock").open("a") as lock:
        lock.write("unknown-launcher-test==1\n")
    assert invoke(*cmd).returncode != 0
    assert invoke(*cmd[:-1]).returncode != 0
    assert not marker.exists()
