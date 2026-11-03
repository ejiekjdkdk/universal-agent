import pytest


@pytest.fixture
def package(tmp_path):
    directory = tmp_path / "lookup"
    directory.mkdir()
    (directory / "SKILL.md").write_text(
        "---\nname: lookup\ndescription: >\n  查找已授权记录。\nallowed-tools: [delete]\n---\n"
        "遵循[查询指南](references/guide.md)。", encoding="utf-8")
    (directory / "references").mkdir()
    (directory / "references/guide.md").write_text("保留证据。", encoding="utf-8")
    (directory / "scripts").mkdir()
    (directory / "scripts/oops.py").write_text("raise RuntimeError('never run')", encoding="utf-8")
    return directory


def test_draft_cannot_grant_capabilities(package, tmp_path):
    from app.skills import SkillRegistry, SkillError
    registry = SkillRegistry(tmp_path / "registry")
    draft = registry.import_package(package)
    assert draft["status"] == "draft"
    assert registry.active("demo") == []
    assert not registry.has_bindings("demo")
    with pytest.raises(SkillError):
        registry.load("demo", "lookup", "1.0.0")
    with pytest.raises(SkillError):
        registry.publish("demo", "lookup", "1.0.0", ["delete"], available_tools=["search"])
    metadata = registry.publish("demo", "lookup", "1.0.0", ["search"], available_tools=["search"])
    assert registry.active("demo") == [metadata]
    assert metadata["tools"] == ["search"]
    assert "body" not in metadata and "references" not in metadata
    loaded = registry.load("demo", "lookup", "1.0.0")
    assert loaded["tools"] == ["search"] and "查询指南" in loaded["body"]
    assert loaded["references"] == ["references/guide.md"]
    assert registry.read_reference("demo", "lookup", "1.0.0", loaded["references"][0]) == "保留证据。"
    with pytest.raises(SkillError):
        registry.read_reference("demo", "lookup", "1.0.0", "../SKILL.md")


def test_pinned_release_and_live_revocation(package, tmp_path):
    from app.skills import SkillRegistry, SkillError
    registry = SkillRegistry(tmp_path / "registry")
    for version in ["1.0.0", "2.0.0"]:
        registry.import_package(package, version)
        registry.publish("demo", "lookup", version)
    registry.publish("other", "lookup", "1.0.0")
    (package / "references/guide.md").write_text("改写", encoding="utf-8")
    with pytest.raises(SkillError):
        registry.import_package(package, "1.0.0")
    with pytest.raises(SkillError):
        registry.publish("demo", "lookup", "1.0.0", ["search"], available_tools=["search"])
    assert registry.active("demo")[0]["version"] == "2.0.0"
    assert registry.read_reference("demo", "lookup", "1.0.0", "references/guide.md") == "保留证据。"
    SkillRegistry(tmp_path / "registry").revoke("demo", "lookup", "1.0.0")
    with pytest.raises(SkillError):
        registry.load("demo", "lookup", "1.0.0")
    with pytest.raises(SkillError):
        registry.publish("demo", "lookup", "1.0.0")
    assert registry.load("other", "lookup", "1.0.0")["tools"] == []
    registry.revoke("demo", "lookup", "2.0.0")
    assert registry.active("demo") == []
    assert registry.has_bindings("demo")


@pytest.mark.parametrize("target", ["../escape.md", "references/missing.md", "references/../SKILL.md",
                                    "C:/Users/example", "references/%2e%2e/SKILL.md"])
def test_import_rejects_unsafe_local_links(package, tmp_path, target):
    from app.skills import SkillRegistry, SkillError
    entry = package / "SKILL.md"
    entry.write_text(entry.read_text(encoding="utf-8").replace("references/guide.md", target), encoding="utf-8")
    with pytest.raises(SkillError):
        SkillRegistry(tmp_path / "registry").import_package(package)


def test_reference_cannot_import_external_hard_link(package, tmp_path):
    import os
    from app.skills import SkillRegistry, SkillError
    private = tmp_path / "private.txt"
    private.write_text("包外数据", encoding="utf-8")
    try:
        os.link(private, package / "references/external.txt")
    except OSError as exc:
        pytest.skip(f"当前测试环境无法创建硬链接：{exc}")
    with pytest.raises(SkillError):
        SkillRegistry(tmp_path / "registry").import_package(package)


def test_manifest_dependencies_do_not_grant_tools_and_schema_must_exist(package, tmp_path):
    from app.skills import SkillRegistry, SkillError
    registry = SkillRegistry(tmp_path / "registry")
    manifest = package / "skill.manifest.yaml"
    manifest.write_text("version: 1.2.0\nstatus: active\nrequired_capabilities: [demo.record.read@1.0.0]\n"
                        "input_schema_ref: schemas/input.json\n", encoding="utf-8")
    with pytest.raises(SkillError):
        registry.import_package(package)
    (package / "schemas").mkdir()
    (package / "schemas/input.json").write_text('{"type":"object"}', encoding="utf-8")
    draft = registry.import_package(package)
    assert draft["version"] == "1.2.0" and registry.active("demo") == []
    assert draft["required_capabilities"] == ["demo.record.read@1.0.0"]
    with pytest.raises(SkillError):
        registry.publish("demo", "lookup", "1.2.0", ["search"], available_tools=["search"])
    with pytest.raises(SkillError):
        registry.publish("demo", "lookup", "1.2.0", ["search"], available_tools=["search"],
                         capability_bindings={"demo.record.read@2.0.0": "search"})
    with pytest.raises(SkillError):
        registry.publish("demo", "lookup", "1.2.0", ["search"], available_tools=["search"],
                         capability_bindings={"demo.record.read@1.0.0": "delete"})
    binding = {"demo.record.read@1.0.0": "search"}
    registry.publish("demo", "lookup", "1.2.0", ["search"], available_tools=["search"], capability_bindings=binding)
    assert registry.load("demo", "lookup", "1.2.0")["capability_bindings"] == binding
    manifest.write_text("skill_id: changed", encoding="utf-8")
    (package / "schemas/input.json").write_text("{}", encoding="utf-8")
    stored = registry.inspect_package("lookup", "1.2.0")
    assert "demo.record.read@1.0.0" in stored["manifest_yaml"]
    assert stored["schemas"] == {"schemas/input.json": {"type": "object"}}


@pytest.mark.parametrize("change", ["duplicate", "directory", "unversioned"])
def test_rejects_ambiguous_identity_or_dependency(package, tmp_path, change):
    from app.skills import SkillRegistry, SkillError
    if change == "directory":
        entry = package / "SKILL.md"
        entry.write_text(entry.read_text(encoding="utf-8").replace("name: lookup", "name: wrong"), encoding="utf-8")
    elif change == "duplicate":
        entry = package / "SKILL.md"
        entry.write_text(entry.read_text(encoding="utf-8").replace("name: lookup", "name: wrong\nname: lookup"),
                         encoding="utf-8")
    else:
        (package / "skill.manifest.yaml").write_text("required_capabilities: [demo.record.read]", encoding="utf-8")
    with pytest.raises(SkillError):
        SkillRegistry(tmp_path / "registry").import_package(package)


def test_cli_binds_only_existing_scenario_capabilities(package, tmp_path, capsys):
    import json
    from scripts.skillctl import main
    args = ["--root", str(tmp_path / "registry")]
    main(args + ["import", str(package)])
    capsys.readouterr()
    publish = args + ["publish", "notes", "lookup", "1.0.0", "--tool"]
    with pytest.raises(SystemExit) as error:
        main(publish + ["delete"])
    assert error.value.code != 0
    main(publish + ["retrieve_knowledge"])
    assert json.loads(capsys.readouterr().out)["tools"] == ["retrieve_knowledge"]
    main(args + ["revoke", "notes", "lookup", "1.0.0"])
    capsys.readouterr()
    main(args + ["list", "notes"])
    assert json.loads(capsys.readouterr().out) == []
