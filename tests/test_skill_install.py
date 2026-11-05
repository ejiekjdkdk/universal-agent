import json
from pathlib import Path
import zipfile

import pytest

from app.skill_devkit import init_skill, pack_skill
from app.skills import SkillRegistry
from scripts.agent import main

KEY = "local-install-test-key-with-32-bytes"


@pytest.fixture
def candidate(tmp_path):
    source = tmp_path / "sample"
    init_skill(source, "notes")
    manifest = source / "skill.manifest.yaml"
    manifest.write_text(manifest.read_text(encoding="utf-8").replace(
        "required_capabilities: []", "author: forged\nrequired_capabilities: [notes.read@1.0.0]"), encoding="utf-8")
    return source


def pack(source, destination, key=KEY):
    return Path(pack_skill(source, destination, ["notes.read@1.0.0"], key.encode() if key else None)["path"])


def install(path, root, monkeypatch, capsys, *, approved=True, key=KEY, author="operator"):
    monkeypatch.setenv("INSTALL_TEST_KEY", key)
    args = ["skill", "install", str(path), "--root", str(root), "--author", author,
            "--signing-key-env", "INSTALL_TEST_KEY"]
    if approved:
        allow = root.parent / "approved.json"
        allow.write_text('["notes.read@1.0.0"]', encoding="utf-8")
        args.extend(["--capabilities", str(allow)])
    code = main(args)
    return code, json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("case", ["unsigned", "wrong-key", "tampered", "unapproved", "unsafe-path"])
def test_invalid_install_leaves_no_registry(candidate, tmp_path, monkeypatch, capsys, case):
    archive = pack(candidate, tmp_path / "out", None if case == "unsigned" else KEY)
    if case in ("tampered", "unsafe-path"):
        modified = tmp_path / "modified.zip"
        with zipfile.ZipFile(archive) as src, zipfile.ZipFile(modified, "w") as dst:
            for item in src.infolist():
                data = src.read(item)
                dst.writestr(item, data + b"x" if case == "tampered" and item.filename == "SKILL.md" else data)
            if case == "unsafe-path":
                dst.writestr("references/../escape.txt", "unsafe")
        archive = modified
    root = tmp_path / "registry"
    code, report = install(archive, root, monkeypatch, capsys, approved=case != "unapproved",
                           key="z"*32 if case == "wrong-key" else KEY)
    assert code == 1 and report["status"] == "FAIL"
    assert not root.exists()


def test_signed_install_is_idempotent_draft_and_rejects_conflicts(candidate, tmp_path, monkeypatch, capsys):
    archive = pack(candidate, tmp_path / "out")
    root = tmp_path / "registry"
    code, result = install(archive, root, monkeypatch, capsys)
    assert code == 0 and result["installation"] == "INSTALLED"
    assert result["publication"] == "draft" and result["authorization"] == "NOT_GRANTED"
    registry = SkillRegistry(root)
    assert not registry.has_bindings("notes")
    assert registry.inspect_package("sample", "1.0.0")["required_capabilities"] == ["notes.read@1.0.0"]
    audit = registry.audit()
    assert len(audit) == 1 and audit[0]["actor"] == "operator"
    assert install(archive, root, monkeypatch, capsys)[1]["installation"] == "ALREADY_INSTALLED"
    assert install(archive, root, monkeypatch, capsys, author="forged")[0] == 1
    (candidate / "tests" / "extra.json").write_text("[]", encoding="utf-8")
    other = pack(candidate, tmp_path / "different")
    assert install(other, root, monkeypatch, capsys)[0] == 1
    assert registry.audit() == audit
    registry.validate("notes", "sample", "1.0.0", ["lookup"], available_tools=["lookup"],
                      capability_bindings={"notes.read@1.0.0": "lookup"}, reviewer="review")
    audit = registry.audit()
    assert install(archive, root, monkeypatch, capsys)[0] == 1
    assert registry.audit() == audit


@pytest.mark.parametrize("bad_name", [
    "references/con.txt", "references/NUL/info.md", "references/note.txt:stream.md",
    "references/trailing./guide.md", "references/trailing /guide.md", "references/../guide.md",
])
def test_paths_rejected_before_content_validation(tmp_path, bad_name):
    from app.skill_devkit import inspect_artifact
    from app.skills import SkillError

    archive = tmp_path / "invalid.zip"
    with zipfile.ZipFile(archive, "w") as dst:
        dst.writestr(bad_name, b"untrusted")
    with pytest.raises(SkillError, match="路径无效"):
        inspect_artifact(archive)


def test_wire_backslash_cannot_alias_signed_member(candidate, tmp_path, monkeypatch, capsys):
    archive = pack(candidate, tmp_path / "out")
    # Replace only ZIP header filenames, leaving the signed JSON payload unmodified.
    with zipfile.ZipFile(archive) as src:
        member = src.getinfo("references/usage.md")
        local_name = member.header_offset + 30
        directory = src.start_dir
    data = bytearray(archive.read_bytes())
    name = member.filename.encode()
    central_name = data.index(name, directory)
    for offset in (local_name, central_name):
        data[offset:offset + len(name)] = name.replace(b"/", b"\\")
    archive.write_bytes(data)
    root = tmp_path / "registry"
    assert install(archive, root, monkeypatch, capsys)[0] == 1
    assert not root.exists()


def test_archive_rejects_directory_case_aliases_before_materialization(tmp_path):
    from app.skill_devkit import inspect_artifact
    from app.skills import SkillError

    archive = tmp_path / "alias.zip"
    with zipfile.ZipFile(archive, "w") as dst:
        dst.writestr("references/DIR/a.md", "one")
        dst.writestr("references/dir/b.md", "two")
    with pytest.raises(SkillError, match="路径.*冲突"):
        inspect_artifact(archive)
