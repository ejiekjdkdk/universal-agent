"""本机发布验收入口；环境检查与完整验收分开，不执行生产发布。"""
import argparse
from contextlib import redirect_stderr, redirect_stdout
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.runtime_environment import sqlite_runtime, sqlite_wal_fixed

REQUIRED_NODES = {
    "V31-020": ["tests/test_skill_install.py::test_signed_install_is_idempotent_draft_and_rejects_conflicts",
                "tests/test_skill_install.py::test_invalid_install_leaves_no_registry[tampered]",
                "tests/test_skill_install.py::test_invalid_install_leaves_no_registry[wrong-key]",
                "tests/test_skill_install.py::test_invalid_install_leaves_no_registry[unsigned]"],
    "WINDOWS-START": ["tests/test_start_runtime.py::test_cmd_checks_registration_and_lock_before_app",
                      "tests/test_start_runtime.py::test_startup_rejects_runtime_drift"],
    "V31-044": ["tests/test_v31_compatibility.py::test_v31_preserves_v3_public_and_database_contracts",
                "tests/test_api.py::", "tests/test_durable_api.py::"],
    "V31-045": ["tests/test_p4_stream_release.py::test_real_disconnect_replays_only_persisted_events[reactive]",
                "tests/test_p4_stream_release.py::test_real_disconnect_replays_only_persisted_events[durable_plan]"],
    "V31-046": ["tests/test_p4_stream_release.py::test_code_and_configuration_rollback_preserve_history",
                "tests/test_skill_governance.py::"],
    "V31-047": ["tests/test_p4_write_recovery.py::test_two_workers_commit_once[normal]",
                "tests/test_p4_write_recovery.py::test_two_workers_commit_once[before_commit]",
                "tests/test_p4_write_recovery.py::test_two_workers_commit_once[after_commit]",
                "tests/test_p4_write_recovery.py::test_two_workers_do_not_resend_unknown_commit",
                "tests/test_p4_write_recovery.py::test_two_workers_respect_cancelled_approval",
                "tests/test_worker_recovery.py::"],
    "V31-048": ["tests/test_p4_stream_release.py::test_code_and_configuration_rollback_preserve_history"],
    "P4-SECURITY": ["tests/test_security.py::", "tests/test_high_risk_context.py::",
                    "tests/test_high_risk_mcp.py::", "tests/test_skill_devkit.py::",
                    "tests/test_mcp_result_security.py::", "tests/test_retrieval_revocation.py::",
                    "tests/test_p3_audit.py::", "tests/test_result_store.py::", "tests/test_replay.py::"],
    "P4-SCENARIOS": ["scenarios/text/test_text.py::test_maximum_length_input_still_returns_valid_output",
                     "scenarios/text/test_text.py::test_text_extension_scopes_tools_and_has_deterministic_counts"],
}


def environment_report() -> dict:
    """只读取运行时与依赖锁，不把环境 PASS 当作版本发布通过。"""
    problems = []
    native = {}
    try:
        native = sqlite_runtime()
    except (OSError, ValueError, RuntimeError):
        problems.append("SQLITE_NATIVE_UNVERIFIED")
    version = native.get("version")
    if not isinstance(version, str) or not sqlite_wal_fixed(version):
        problems.append("SQLITE_WAL_UNPATCHED")
    for field in ("extension_sha256", "library_sha256"):
        digest = native.get(field)
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            problems.append("SQLITE_NATIVE_UNVERIFIED")
    if native.get("threadsafety") != 3 or not native.get("source_id"):
        problems.append("SQLITE_NATIVE_UNVERIFIED")
    if os.name != "nt" or sys.version_info[:2] != (3, 11) or struct.calcsize("P") != 8:
        problems.append("UNVERIFIED_PLATFORM")
    mismatches = []
    packages = {}
    lock_digest = None
    try:
        raw = (ROOT / "requirements-win-py311.lock").read_bytes()
        lock_digest = hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()
        for line in raw.decode("utf-8").splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            name, separator, expected = line.partition("==")
            normalized = re.sub(r"[-_.]+", "-", name).lower()
            if (not separator or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name)
                    or not expected or normalized in packages):
                raise ValueError("发布依赖锁必须固定版本")
            try:
                installed = metadata.version(name)
            except metadata.PackageNotFoundError:
                installed = None
            packages[normalized] = installed
            if installed != expected:
                mismatches.append({"name": name, "expected": expected, "installed": installed})
        if not packages:
            raise ValueError("发布依赖锁不能为空")
    except (OSError, ValueError, UnicodeError):
        problems.append("DEPENDENCY_LOCK_UNREADABLE")
    if mismatches:
        problems.append("DEPENDENCY_LOCK_MISMATCH")
    return {"schema_version": "p4-environment-1", "phase": "environment",
            "status": "FAIL" if problems else "PASS", "release_status": "NOT_RUN",
            "production_status": "NOT_RUN", "python": sys.version.split()[0], "bits": struct.calcsize("P") * 8,
            "sqlite": native, "lock_sha256": lock_digest, "packages": packages,
            "lock_mismatches": mismatches, "problems": sorted(set(problems))}


def summarize_suite(expected: list[str], reports: dict[str, str], exit_code: int,
                    required: dict[str, list[str]], *, collection_reports: dict[str, str] | None = None,
                    deselected: list[str] | None = None) -> dict:
    """用实际收集/执行结果判断门禁；代码存在、跳过或只跑部分均不能就绪。"""
    problems = []
    collection_reports = collection_reports or {}
    deselected = deselected or []
    if not expected or len(expected) != len(set(expected)):
        problems.append("EMPTY_OR_DUPLICATE_SUITE")
    if set(expected) != set(reports) or any(value not in {"PASS", "FAIL", "SKIP"} for value in reports.values()):
        problems.append("INCOMPLETE_SUITE")
    if exit_code != 0:
        problems.append("PYTEST_EXIT_NONZERO")
    if any(value != "PASS" for value in reports.values()):
        problems.append("TEST_FAILED_OR_SKIPPED")
    if collection_reports:
        problems.append("COLLECTION_FAILED_OR_SKIPPED")
    if deselected:
        problems.append("TESTS_DESELECTED")
    cases: dict[str, dict] = {}
    for case_id, patterns in required.items():
        matched = set()
        missing = []
        for pattern in patterns:
            nodes = [node for node in expected if node == pattern or node.startswith(pattern + "[")
                     or pattern.endswith("::") and node.startswith(pattern)]
            if not nodes:
                missing.append(pattern)
            matched.update(nodes)
        outcomes = [reports.get(node, "NOT_RUN") for node in matched]
        state = ("BLOCKED" if missing or not matched or "NOT_RUN" in outcomes else
                 "PASS" if all(value == "PASS" for value in outcomes) else "FAIL")
        cases[case_id] = {"status": state, "nodes": sorted(matched), "missing": missing}
    if not required or any(case["status"] != "PASS" for case in cases.values()):
        problems.append("REQUIRED_CASES_INCOMPLETE")
    return {"status": "FAIL" if problems else "PASS",
            "release_status": "BLOCKED" if problems else "LOCAL_RELEASE_READY", "production_status": "NOT_RUN",
            "cases": cases, "problems": problems, "exit_code": exit_code,
            "counts": {"collected": len(expected), "passed": sum(v == "PASS" for v in reports.values()),
                       "failed": sum(v == "FAIL" for v in reports.values())
                                 + sum(v == "FAIL" for v in collection_reports.values()),
                       "skipped": sum(v == "SKIP" for v in reports.values())
                                  + sum(v == "SKIP" for v in collection_reports.values()),
                       "deselected": len(deselected),
                       "collection_failed": sum(v == "FAIL" for v in collection_reports.values()),
                       "collection_skipped": sum(v == "SKIP" for v in collection_reports.values())}}


class SuiteEvidence:
    """记录收集、取消选择与运行；收集异常不冒充已知数量的测试节点。"""
    def __init__(self):
        self.expected: list[str] = []
        self.reports: dict[str, str] = {}
        self.collection_reports: dict[str, str] = {}
        self.deselected: list[str] = []

    def pytest_collectreport(self, report):
        if report.failed:
            self.collection_reports[report.nodeid] = "FAIL"
        elif report.skipped and self.collection_reports.get(report.nodeid) != "FAIL":
            self.collection_reports[report.nodeid] = "SKIP"

    def pytest_deselected(self, items):
        self.deselected.extend(item.nodeid for item in items)

    def pytest_collection_finish(self, session):
        self.expected = [item.nodeid for item in session.items] + self.deselected

    def pytest_runtest_logreport(self, report):
        current = self.reports.get(report.nodeid)
        if report.failed:
            self.reports[report.nodeid] = "FAIL"
        elif report.skipped and current != "FAIL":
            self.reports[report.nodeid] = "SKIP"
        elif report.when == "call" and current not in {"FAIL", "SKIP"}:
            self.reports[report.nodeid] = "PASS"


def _git(*args):
    return subprocess.check_output(["git", "--no-optional-locks", *args], cwd=ROOT,
                                   stderr=subprocess.PIPE, text=True, encoding="utf-8").strip()


def _quality_checks(output: Path) -> list[dict]:
    commands = [
        [sys.executable, "-m", "pip", "check"],
        [sys.executable, "-m", "compileall", "-q", "app", "scripts", "tests", "examples"],
        ["node", "--check", "static/app.js"], ["node", "--check", "static/review.js"],
        [sys.executable, "-m", "mypy", "app/runtime_environment.py", "app/replay.py", "scripts/release_check.py",
         "--follow-imports=skip", "--ignore-missing-imports", "--check-untyped-defs", "--cache-dir", str(output / "mypy-cache")],
        ["git", "diff", "--check"],
    ]
    results = []
    for number, command in enumerate(commands):
        log = output / f"check-{number}.log"
        try:
            result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=180)
            log.write_text(result.stdout + result.stderr, encoding="utf-8")
            code = result.returncode
        except (OSError, subprocess.TimeoutExpired):
            log.write_text("检查未完成，请核对工具可用性和执行时限。\n", encoding="utf-8")
            code = -1
        results.append({"command": command, "status": "PASS" if code == 0 else "FAIL", "exit_code": code,
                        "evidence": log.name})
    return results


def _run_suite(output: Path, sandbox_temp: bool) -> dict:
    """独立 CLI 中执行受信测试；记录真实收集/执行与临时目录模式。"""
    import pytest
    evidence = SuiteEvidence()
    plugins: list[object] = [evidence]
    temp_context = None
    if sandbox_temp:
        if os.name != "nt":
            raise ValueError("sandbox-temp 只用于已验证的 Windows 沙箱")
        # 默认 Temp 仍由操作者/沙箱限定；短目录不触碰产品数据和权限。
        base = Path(tempfile.gettempdir()) / ("p4-" + os.urandom(3).hex())
        base.mkdir()
        class SandboxPaths:
            def __init__(self):
                self.sequence = 0

            @pytest.fixture
            def tmp_path(self):
                self.sequence += 1
                path = base / f"t{self.sequence}"
                path.mkdir()
                return path
        plugins.append(SandboxPaths())
        temp_context = str(base / "pytest")
    # 固定本地受信配置并从仓库根完整发现；上级 ini/conftest 不能在收集前过滤模块。
    configuration = output / "pytest.ini"
    configuration.write_text("[pytest]\naddopts =\n", encoding="utf-8")
    arguments = ["-c", str(configuration), "--rootdir", str(ROOT), "--confcutdir", str(ROOT),
                 ".", "-q", "-ra", "--tb=short", "--junitxml=" + str(output / "pytest.xml")]
    if temp_context:
        arguments += ["--basetemp", temp_context]
    old_directory = Path.cwd()
    saved_environment = {key: value for key, value in os.environ.items() if key.startswith(("AGENT_", "PYTEST_"))}
    for key in saved_environment:
        os.environ.pop(key)
    started = time.monotonic()
    try:
        os.chdir(ROOT)
        with (output / "pytest.log").open("w", encoding="utf-8") as log, redirect_stdout(log), redirect_stderr(log):
            code = int(pytest.main(arguments, plugins=plugins))
    finally:
        os.chdir(old_directory)
        for key in list(os.environ):
            if key.startswith(("AGENT_", "PYTEST_")):
                os.environ.pop(key)
        os.environ.update(saved_environment)
    result = summarize_suite(evidence.expected, evidence.reports, code, REQUIRED_NODES,
                             collection_reports=evidence.collection_reports, deselected=evidence.deselected)
    result["duration_seconds"] = round(time.monotonic() - started, 3)
    result["pytest_arguments"] = arguments
    return result


def _source_identity() -> dict:
    if _git("status", "--porcelain=v1", "--untracked-files=all"):
        raise ValueError("WORKTREE_DIRTY")
    return {"commit_sha": _git("rev-parse", "HEAD"), "tree_sha": _git("rev-parse", "HEAD^{tree}")}


def verify_release(output: Path, *, sandbox_temp: bool = False) -> dict:
    """仅限独立同步 CLI；执行受信仓库测试，不可嵌入在线 API 进程。"""
    output = output.resolve()
    if output.exists() or output.is_symlink() or output.is_relative_to(ROOT.resolve()):
        raise ValueError("证据必须写入源码目录外的全新专用目录")
    report: dict = {"schema_version": "p4-release-1", "phase": "local_verification", "status": "FAIL",
                    "release_status": "BLOCKED", "production_status": "NOT_RUN", "suite": {"status": "NOT_RUN"}}
    environment = environment_report()
    report["environment"] = environment
    output.mkdir(parents=True)

    def save():
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if environment["status"] != "PASS":
        save()
        return report
    try:
        if (ROOT / ".env").exists():
            raise ValueError("LOCAL_ENV_FILE_PRESENT")
        identity = _source_identity()
        report.update(identity)
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        report["problems"] = [str(error) if isinstance(error, ValueError) else "GIT_IDENTITY_UNAVAILABLE"]
        save()
        return report
    report["suite"] = {"status": "BLOCKED", "message": "验收尚未完成"}
    save()
    report["suite"] = _run_suite(output, sandbox_temp)
    report["command"] = [sys.executable, str(Path(__file__).resolve()), "verify", "--output", str(output)]
    if sandbox_temp:
        report["command"].append("--sandbox-temp")
    report["sandbox_temp"] = sandbox_temp
    report["checks"] = _quality_checks(output)
    if report["suite"]["status"] == "PASS" and all(item["status"] == "PASS" for item in report["checks"]):
        report["status"] = "PASS"
        report["release_status"] = "LOCAL_RELEASE_READY"
    try:
        if _source_identity() != identity:
            raise ValueError("SOURCE_CHANGED")
    except (OSError, subprocess.SubprocessError, ValueError):
        report.update(status="FAIL", release_status="BLOCKED", problems=["SOURCE_CHANGED"])
    fixtures = [ROOT / "tests/fixtures/v31-baseline.json", ROOT / "examples/v3_1/p3/replay-case.json"]
    report["fixture_sha256"] = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                                for path in fixtures if path.is_file()}
    report["artifact_sha256"] = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                                 for path in output.iterdir() if path.is_file() and path.name != "report.json"}
    save()
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("environment", help="核验本机运行时和原依赖锁，不运行测试或发布")
    verify = commands.add_parser("verify", help="冻结代码身份后运行完整本机验收，不发布")
    verify.add_argument("--output", type=Path, required=True, help="源码外全新证据目录")
    verify.add_argument("--sandbox-temp", action="store_true", help="Windows AppContainer 短临时路径兼容")
    args = parser.parse_args(argv)
    try:
        report = environment_report() if args.command == "environment" else verify_release(args.output, sandbox_temp=args.sandbox_temp)
    except (OSError, ValueError) as error:
        report = {"status": "FAIL", "release_status": "BLOCKED", "production_status": "NOT_RUN",
                  "error": type(error).__name__, "message": "验收未执行或未完成，请检查环境、干净源码和全新证据目录。"}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    configure_stdout = getattr(sys.stdout, "reconfigure", None)
    if configure_stdout:
        configure_stdout(encoding="utf-8")
    raise SystemExit(main())
