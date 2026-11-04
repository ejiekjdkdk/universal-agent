"""本机发布环境必须证明实际原生库和锁定依赖，环境通过不等于生产就绪。"""
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("version,allowed", [
    ("3.39.4", False), ("3.44.5", False), ("3.44.6", True), ("3.44.7", True),
    ("3.45.0", False), ("3.50.6", False), ("3.50.7", True), ("3.51.2", False),
    ("3.51.3", True), ("3.53.1", True), ("unknown", False), ("3.51.3-rc1", False),
])
def test_release_accepts_only_fixed_sqlite_branches(version, allowed):
    runtime = importlib.import_module("app.runtime_environment")
    assert runtime.sqlite_wal_fixed(version) is allowed


def test_environment_gate_blocks_vulnerable_native_sqlite(monkeypatch):
    check = importlib.import_module("scripts.release_check")
    actual = check.sqlite_runtime()
    monkeypatch.setattr(check, "sqlite_runtime", lambda: {**actual, "version": "3.39.4"})
    result = check.environment_report()
    assert result["status"] == "FAIL"
    assert "SQLITE_WAL_UNPATCHED" in result["problems"]
    assert result["release_status"] == "NOT_RUN"


def test_environment_gate_requires_native_binary_proof(monkeypatch):
    check = importlib.import_module("scripts.release_check")
    actual = check.sqlite_runtime()
    monkeypatch.setattr(check, "sqlite_runtime", lambda: {**actual, "library_sha256": None})
    result = check.environment_report()
    assert result["status"] == "FAIL" and "SQLITE_NATIVE_UNVERIFIED" in result["problems"]


def test_environment_gate_blocks_locked_package_mismatch(monkeypatch):
    check = importlib.import_module("scripts.release_check")
    original = check.metadata.version
    monkeypatch.setattr(check.metadata, "version", lambda name: "0.0.0" if name == "mcp" else original(name))
    result = check.environment_report()
    assert result["status"] == "FAIL" and "DEPENDENCY_LOCK_MISMATCH" in result["problems"]
    assert any(row["name"] == "mcp" and row["installed"] == "0.0.0" for row in result["lock_mismatches"])


def test_environment_cli_does_not_claim_full_release_or_leak_credentials():
    env = {**os.environ, "AGENT_ADMIN_TOKEN": "fictional-secret-not-for-release-output"}
    result = subprocess.run([sys.executable, str(ROOT / "scripts/release_check.py"), "environment"],
                            capture_output=True, text=True, encoding="utf-8", env=env, timeout=30)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["status"] == "PASS" and report["phase"] == "environment"
    assert report["release_status"] == report["production_status"] == "NOT_RUN"
    assert "fictional-secret-not-for-release-output" not in result.stdout + result.stderr


@pytest.mark.parametrize("content", ["", "# no pins\n", "mcp==2.3.0\nmcp==2.3.0\n", "mcp>=2.3\n"])
def test_environment_gate_rejects_empty_or_ambiguous_lock(tmp_path, monkeypatch, content):
    check = importlib.import_module("scripts.release_check")
    (tmp_path / "requirements-win-py311.lock").write_text(content, encoding="utf-8")
    monkeypatch.setattr(check, "ROOT", tmp_path)
    result = check.environment_report()
    assert result["status"] == "FAIL" and "DEPENDENCY_LOCK_UNREADABLE" in result["problems"]


def suite_fixture():
    required = {"V31-044": ["contract"], "V31-045": ["stream/reactive", "stream/durable"],
                "V31-046": ["history"], "V31-047": ["write/before", "write/after"], "V31-048": ["rollback"]}
    expected = ["contract", "stream/reactive", "stream/durable", "history", "write/before", "write/after", "rollback"]
    return required, expected, dict.fromkeys(expected, "PASS")


def test_release_summary_requires_complete_actual_execution():
    check = importlib.import_module("scripts.release_check")
    required, expected, reports = suite_fixture()
    result = check.summarize_suite(expected, reports, 0, required)
    assert result["status"] == "PASS" and result["release_status"] == "LOCAL_RELEASE_READY"
    assert result["production_status"] == "NOT_RUN"
    assert all(row["status"] == "PASS" for row in result["cases"].values())


@pytest.mark.parametrize("fault", ["missing_collection", "missing_result", "failed", "skipped", "interrupted", "empty"])
def test_release_summary_never_turns_incomplete_evidence_into_ready(fault):
    check = importlib.import_module("scripts.release_check")
    required, expected, reports = suite_fixture()
    code = 0
    if fault == "missing_collection":
        expected.remove("write/before")
        reports.pop("write/before")
    elif fault == "missing_result":
        reports.pop("write/before")
    elif fault == "failed":
        reports["write/before"] = "FAIL"
    elif fault == "skipped":
        reports["write/before"] = "SKIP"
    elif fault == "interrupted":
        code = 2
    else:
        expected.clear()
        reports.clear()
    result = check.summarize_suite(expected, reports, code, required)
    assert result["status"] == "FAIL" and result["release_status"] == "BLOCKED"
    assert result["production_status"] == "NOT_RUN"


def test_release_verifier_refuses_existing_evidence_directory(tmp_path):
    check = importlib.import_module("scripts.release_check")
    marker = tmp_path / "keep.txt"
    marker.write_text("existing-evidence", encoding="utf-8")
    with pytest.raises(ValueError):
        check.verify_release(tmp_path, sandbox_temp=True)
    assert marker.read_text(encoding="utf-8") == "existing-evidence"


def test_release_verifier_stops_before_tests_when_environment_is_invalid(tmp_path, monkeypatch):
    check = importlib.import_module("scripts.release_check")
    monkeypatch.setattr(check, "environment_report", lambda: {"status": "FAIL", "problems": ["SQLITE_WAL_UNPATCHED"]})
    result = check.verify_release(tmp_path / "new", sandbox_temp=True)
    assert result["status"] == "FAIL" and result["release_status"] == "BLOCKED"
    assert result["suite"]["status"] == "NOT_RUN" and result["production_status"] == "NOT_RUN"


def test_release_verifier_refuses_local_dotenv_before_running_tests(tmp_path, monkeypatch):
    check = importlib.import_module("scripts.release_check")
    root = tmp_path / "source"
    root.mkdir()
    (root / ".env").write_text("AGENT_MODEL_API_KEY=fictional-do-not-load", encoding="utf-8")
    monkeypatch.setattr(check, "ROOT", root)
    monkeypatch.setattr(check, "environment_report", lambda: {"status": "PASS"})
    result = check.verify_release(tmp_path / "evidence")
    assert "LOCAL_ENV_FILE_PRESENT" in result["problems"] and result["suite"]["status"] == "NOT_RUN"


def test_release_verifier_blocks_checkout_changed_during_verification(tmp_path, monkeypatch):
    check = importlib.import_module("scripts.release_check")
    root = tmp_path / "source"
    root.mkdir()
    monkeypatch.setattr(check, "ROOT", root)
    monkeypatch.setattr(check, "environment_report", lambda: {"status": "PASS"})
    identities = iter([{"commit_sha": "a" * 40, "tree_sha": "b" * 40},
                       {"commit_sha": "c" * 40, "tree_sha": "d" * 40}])
    monkeypatch.setattr(check, "_source_identity", lambda: next(identities), raising=False)
    monkeypatch.setattr(check, "_run_suite", lambda *_: {"status": "PASS"}, raising=False)
    monkeypatch.setattr(check, "_quality_checks", lambda *_: [{"status": "PASS"}])
    result = check.verify_release(tmp_path / "evidence")
    assert result["status"] == "FAIL" and result["release_status"] == "BLOCKED"
    assert "SOURCE_CHANGED" in result["problems"] and result["production_status"] == "NOT_RUN"


def run_toy_release_suite(tmp_path, extra, *, parent_config=None, parent_conftest=None,
                          source_conftest=None, required_body="    assert True\n", source_files=None):
    """新解释器实际运行小套件，避免嵌套 pytest 的模块/插件缓存污染。"""
    source = tmp_path / "source"
    tests = source / "tests"
    tests.mkdir(parents=True)
    (tests / "test_required.py").write_text("def test_required(subtests):\n" + required_body, encoding="utf-8")
    (tests / "test_extra.py").write_text(extra, encoding="utf-8")
    for relative, content in (source_files or {}).items():
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    if parent_config:
        (tmp_path / "pytest.ini").write_text("[pytest]\n" + parent_config, encoding="utf-8")
    if parent_conftest:
        (tmp_path / "conftest.py").write_text(parent_conftest, encoding="utf-8")
    if source_conftest:
        (source / "conftest.py").write_text(source_conftest, encoding="utf-8")
    output = tmp_path / "evidence"
    output.mkdir()
    runner = textwrap.dedent("""
        import json
        from pathlib import Path
        import sys
        sys.dont_write_bytecode = True
        sys.path.insert(0, sys.argv[1])
        from scripts import release_check as check
        check.ROOT = Path(sys.argv[2])
        check.REQUIRED_NODES = {"REQUIRED": ["tests/test_required.py::test_required"]}
        result = check._run_suite(Path(sys.argv[3]), False)
        print(json.dumps(result))
    """)
    completed = subprocess.run([sys.executable, "-c", runner, str(ROOT), str(source), str(output)],
                               cwd=source, capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "fictional-host-config-secret" not in completed.stdout + completed.stderr
    return json.loads(completed.stdout)


@pytest.mark.parametrize("extra,exit_code,outcome", [
    ('import pytest\npytest.skip("module skip", allow_module_level=True)\n', 0, "skipped"),
    ('import pytest\npytest.importorskip("fictional_missing_p4_dependency")\n', 0, "skipped"),
    ('raise RuntimeError("collection failure")\n', 2, "failed"),
])
def test_release_real_pytest_blocks_collection_skip_or_failure(tmp_path, extra, exit_code, outcome):
    result = run_toy_release_suite(tmp_path, extra)
    assert result["exit_code"] == exit_code
    assert result["status"] == "FAIL" and result["release_status"] == "BLOCKED"
    assert result["counts"]["passed"] == (1 if exit_code == 0 else 0)
    assert result["counts"]["collection_" + outcome] == 1
    assert result["counts"][outcome] == 1


def test_release_real_pytest_blocks_deselection_even_with_exit_zero(tmp_path):
    conftest = textwrap.dedent("""
        def pytest_collection_modifyitems(config, items):
            removed = [item for item in items if item.name == "test_extra"]
            items[:] = [item for item in items if item.name != "test_extra"]
            config.hook.pytest_deselected(items=removed)
    """)
    result = run_toy_release_suite(tmp_path, "def test_extra():\n    assert False\n",
                                  source_conftest=conftest)
    assert result["exit_code"] == 0
    assert result["status"] == "FAIL" and result["release_status"] == "BLOCKED"
    assert result["counts"]["collected"] == 2 and result["counts"]["deselected"] == 1
    assert result["counts"]["passed"] == 1


@pytest.mark.parametrize("host_filter", ["addopts", "testpaths", "ignore", "conftest"])
def test_release_real_pytest_runs_full_tests_despite_parent_configuration(tmp_path, host_filter):
    configs = {"addopts": "addopts = -k required\n",
               "testpaths": "testpaths = source/tests/test_required.py\n",
               "ignore": "addopts = --ignore=tests/test_extra.py\n",
               "conftest": "addopts =\n"}
    parent_conftest = ('raise RuntimeError("fictional-host-config-secret")\n'
                       if host_filter == "conftest" else None)
    result = run_toy_release_suite(tmp_path, "def test_extra():\n    assert False\n",
                                  parent_config=configs.get(host_filter), parent_conftest=parent_conftest)
    assert result["exit_code"] == 1
    assert result["status"] == "FAIL" and result["release_status"] == "BLOCKED"
    assert result["counts"]["collected"] == 2
    assert result["counts"]["passed"] == result["counts"]["failed"] == 1
    assert result["counts"]["deselected"] == result["counts"]["skipped"] == 0
    assert result["cases"]["REQUIRED"]["status"] == "PASS"


def test_release_real_pytest_accepts_only_complete_passing_suite(tmp_path):
    result = run_toy_release_suite(tmp_path, "def test_extra():\n    assert True\n")
    assert result["exit_code"] == 0 and result["status"] == "PASS"
    assert result["release_status"] == "LOCAL_RELEASE_READY" and result["production_status"] == "NOT_RUN"
    assert result["counts"] == {"collected": 2, "passed": 2, "failed": 0, "skipped": 0,
                                "deselected": 0, "collection_failed": 0, "collection_skipped": 0}


def test_release_real_pytest_keeps_failed_subtests_blocking(tmp_path):
    result = run_toy_release_suite(tmp_path, "def test_extra():\n    assert True\n",
                                  required_body='    with subtests.test(case="failed"):\n        assert False\n')
    assert result["exit_code"] == 1
    assert result["status"] == "FAIL" and result["release_status"] == "BLOCKED"
    assert result["counts"]["failed"] == result["counts"]["passed"] == 1


def test_release_real_pytest_discovers_scenarios_outside_tests(tmp_path):
    result = run_toy_release_suite(tmp_path, "def test_extra():\n    assert True\n",
                                  source_files={"scenarios/text/test_scenario.py":
                                                "def test_scenario():\n    assert False\n"})
    assert result["exit_code"] == 1
    assert result["status"] == "FAIL" and result["release_status"] == "BLOCKED"
    assert result["counts"]["collected"] == 3
    assert result["counts"]["passed"] == 2 and result["counts"]["failed"] == 1
    assert result["counts"]["deselected"] == result["counts"]["skipped"] == 0
    assert result["cases"]["REQUIRED"]["status"] == "PASS"
