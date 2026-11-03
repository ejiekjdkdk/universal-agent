"""通过实际文件和 CLI 验证技能候选工件，不执行包内代码。"""
import importlib
import json
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

from app.skills import SkillError, SkillRegistry


ROOT = Path(__file__).resolve().parents[1]


def devkit():
    try:
        return importlib.import_module("app.skill_devkit")
    except ModuleNotFoundError:
        pytest.fail("尚未实现 Skill DevKit")


def package(tmp_path):
    module = devkit()
    path = tmp_path / "sample-skill"
    module.init_skill(path, "notes")
    return module, path


def test_init_does_not_overwrite_and_registry_accepts_generated_package(tmp_path):
    module, path = package(tmp_path)
    before = (path / "SKILL.md").read_bytes()
    with pytest.raises((SkillError, FileExistsError)):
        module.init_skill(path, "notes")
    assert (path / "SKILL.md").read_bytes() == before
    registry = SkillRegistry(tmp_path / "registry")
    result = registry.import_package(path)
    assert result["name"] == "sample-skill"
    assert registry.active("notes") == []


def test_declared_dependency_needs_operator_approved_list(tmp_path):
    module, path = package(tmp_path)
    manifest = path / "skill.manifest.yaml"
    text = manifest.read_text(encoding="utf-8").replace("required_capabilities: []", "required_capabilities: [notes.read@1.0.0]")
    manifest.write_text(text, encoding="utf-8")
    with pytest.raises(SkillError):
        module.validate_skill(path, [])
    assert module.validate_skill(path, ["notes.read@1.0.0"])["status"] == "PASS"


def test_contract_tests_report_failure_and_missing_cases_honestly(tmp_path):
    module, path = package(tmp_path)
    report = module.test_skill(path, [])
    assert report["status"] == "PASS" and report["test_level"] == "contract"
    assert {row["status"] for row in report["cases"]} == {"PASS"}
    fixture = path / "tests" / "cases.json"
    fixture.write_text(json.dumps([{"id": "invalid-expectation", "target": "input", "value": {}, "valid": True}]), encoding="utf-8")
    assert module.test_skill(path, [])["status"] == "FAIL"
    fixture.unlink()
    assert module.test_skill(path, [])["status"] == "NOT_RUN"


@pytest.mark.parametrize("name,content", [("scripts/run.py", "raise RuntimeError('不要执行')"),
                                          (".env", "PRIVATE=secret"),
                                          ("references/key.txt", "api_key: fictional-secret-value-123456"),
                                          ("references/key.pem", "-----BEGIN PRIVATE KEY-----")])
def test_package_rejects_executable_or_credential_files(tmp_path, name, content):
    module, path = package(tmp_path)
    unsafe = path / name
    unsafe.parent.mkdir(exist_ok=True)
    unsafe.write_text(content, encoding="utf-8")
    with pytest.raises(SkillError):
        module.pack_skill(path, tmp_path / "out", [])


def test_same_input_produces_identical_archive_and_optional_signature(tmp_path):
    module, path = package(tmp_path)
    key = b"local-test-signing-key-not-production" * 2
    first = module.pack_skill(path, tmp_path / "one", [], signing_key=key)
    second = module.pack_skill(path, tmp_path / "two", [], signing_key=key)
    assert Path(first["path"]).read_bytes() == Path(second["path"]).read_bytes()
    checked = module.inspect_artifact(Path(first["path"]), signing_key=key)
    assert checked["status"] == "PASS" and checked["signature"] == "verified"
    assert checked["package_digest"] == first["package_digest"]
    with pytest.raises(SkillError):
        module.inspect_artifact(Path(first["path"]), signing_key=b"incorrect-signing-key" * 2)


def test_tampered_artifact_is_rejected_without_extracting_files(tmp_path):
    module, path = package(tmp_path)
    packed = module.pack_skill(path, tmp_path / "out", [])
    altered = tmp_path / "altered.zip"
    with zipfile.ZipFile(packed["path"]) as original, zipfile.ZipFile(altered, "w") as archive:
        for entry in original.infolist():
            data = original.read(entry)
            if entry.filename == "SKILL.md":
                data += b"changed"
            archive.writestr(entry, data)
    with pytest.raises(SkillError):
        module.inspect_artifact(altered)
    assert not (tmp_path / "SKILL.md").exists()


@pytest.mark.parametrize("unsafe", ["../escape.txt", "/absolute.txt", "references/../escape.txt", "references\\escape.txt"])
def test_archive_rejects_unsafe_paths_before_reading_payload(tmp_path, unsafe):
    module = devkit()
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as stream:
        stream.writestr(unsafe, "ignored")
    with pytest.raises(SkillError):
        module.inspect_artifact(archive)
    assert not (tmp_path / "escape.txt").exists()


def test_archive_rejects_duplicate_names_and_high_compression(tmp_path):
    module = devkit()
    duplicate = tmp_path / "duplicate.zip"
    with zipfile.ZipFile(duplicate, "w") as stream:
        stream.writestr("SKILL.md", "one")
        with pytest.warns(UserWarning):
            stream.writestr("SKILL.md", "two")
    with pytest.raises(SkillError):
        module.inspect_artifact(duplicate)
    bomb = tmp_path / "bomb.zip"
    with zipfile.ZipFile(bomb, "w", compression=zipfile.ZIP_DEFLATED) as stream:
        stream.writestr("references/large.txt", "x" * 2_000_000)
    with pytest.raises(SkillError):
        module.inspect_artifact(bomb)


def test_hardlink_cannot_import_content_outside_package(tmp_path):
    module, path = package(tmp_path)
    external = tmp_path / "private.txt"
    external.write_text("不要打包", encoding="utf-8")
    (path / "references" / "outside.txt").hardlink_to(external)
    with pytest.raises(SkillError):
        module.pack_skill(path, tmp_path / "out", [])


def test_cli_commands_are_real_and_do_not_publish(tmp_path):
    command = [sys.executable, str(ROOT / "scripts" / "agent.py")]
    destination = tmp_path / "cli-skill"
    result = subprocess.run(command + ["skill", "init", str(destination), "--business", "notes"],
                            capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "PASS"
    for arguments in (["skill", "validate", str(destination)], ["skill", "test", str(destination), "--offline"],
                      ["doctor", "--scope", "local"]):
        result = subprocess.run(command + arguments, capture_output=True, text=True, encoding="utf-8", timeout=30)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["status"] == "PASS"
    assert not list(tmp_path.rglob("skills.sqlite3"))


@pytest.mark.parametrize("schema_version", [None, "3.0"])
def test_devkit_accepts_legacy_manifest_versions(tmp_path, schema_version):
    module, path = package(tmp_path)
    manifest = path / "skill.manifest.yaml"
    text = manifest.read_text(encoding="utf-8")
    if schema_version is None:
        text = text.replace('schema_version: "3.1"\n', "")
    else:
        text = text.replace('schema_version: "3.1"', 'schema_version: "3.0"')
    manifest.write_text(text, encoding="utf-8")
    assert module.validate_skill(path, [])["status"] == "PASS"


@pytest.mark.parametrize("target,text", [("skill.manifest.yaml", "invalid: ["),
                                        ("schemas/input.schema.json", '{"type": "nonexistent"}')])
def test_invalid_package_reports_structured_cli_error_without_traceback(tmp_path, target, text):
    _, path = package(tmp_path)
    (path / target).write_text(text, encoding="utf-8")
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / "agent.py"), "skill", "validate", str(path)],
                            capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 1
    assert result.stdout.strip(), result.stderr
    assert json.loads(result.stdout)["status"] == "FAIL"
    assert "Traceback" not in result.stderr


def test_non_ascii_signature_is_rejected_as_package_error(tmp_path):
    module, path = package(tmp_path)
    packed = module.pack_skill(path, tmp_path / "out", [])
    altered = tmp_path / "signature.zip"
    with zipfile.ZipFile(packed["path"]) as original, zipfile.ZipFile(altered, "w") as output:
        for info in original.infolist():
            raw = original.read(info)
            if info.filename == "artifact.json":
                descriptor = json.loads(raw)
                descriptor["signature"] = "伪造签名"
                raw = json.dumps(descriptor).encode("utf-8")
            output.writestr(info, raw)
    with pytest.raises(SkillError):
        module.inspect_artifact(altered, signing_key=b"local-verification-test-key" * 2)


def test_doctor_reports_incompatible_locked_sdk_without_changing_environment(monkeypatch):
    module = devkit()
    installed = module.metadata.version
    monkeypatch.setattr(module.metadata, "version", lambda name: "0.0.0" if name == "mcp" else installed(name))
    report = module.doctor()
    assert report["status"] == "FAIL"
    assert any(row["package"] == "mcp" for row in report["lock_mismatches"])
