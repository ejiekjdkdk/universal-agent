import asyncio
import json
import shutil
from pathlib import Path

import httpx
import pytest

from app.catalog import Catalog
from app.errors import Fault


SCENARIOS = Path(__file__).resolve().parents[1] / "scenarios"


def run(catalog, scenario, tool, arguments):
    return asyncio.run(catalog.execute(scenario, tool, arguments))


def write_manifest(folder, manifest):
    (folder / "scenario.json").write_text(json.dumps(manifest), encoding="utf-8")


@pytest.fixture
def notes(tmp_path):
    folder = tmp_path / "notes"
    shutil.copytree(SCENARIOS / "notes", folder)
    return folder, json.loads((folder / "scenario.json").read_text(encoding="utf-8"))


def test_empty_catalog(tmp_path):
    assert Catalog(tmp_path).scenarios == {}


def test_deleted_scenarios_directory_is_empty(tmp_path):
    assert Catalog(tmp_path / "deleted").scenarios == {}


def test_discovery_is_scoped_and_hides_adapter_config(notes):
    folder, _ = notes
    catalog = Catalog(folder.parent)
    assert set(catalog.entries) == {"notes"}
    tools = catalog.discover("notes")
    assert [tool["name"] for tool in tools] == ["notes_search"]
    assert "adapter" not in tools[0]
    with pytest.raises(Fault):
        catalog.discover("absent")


@pytest.mark.parametrize("change", [
    lambda m: m.update(version="v1"),
    lambda m: m.update(typo=True),
    lambda m: m.update(offline_tool="missing"),
    lambda m: m["tools"][0]["input_schema"]["properties"]["text"].update(type="integer"),
    lambda m: m["tools"][0]["input_schema"]["required"].append("other"),
    lambda m: m["tools"][0]["output_schema"]["required"].remove("summary"),
    lambda m: m["tools"].append(m["tools"][0].copy()),
    lambda m: m["tools"][0].update(adapter={"type": "shell"}),
    lambda m: m["tools"][0].update(input_schema={"type": "object"}),
    lambda m: m["tools"][0]["input_schema"]["properties"].update(x={"$ref": "https://invalid.test/x"}),
    lambda m: m["tools"][0]["input_schema"]["properties"].update(x=True),
    lambda m: m["tools"][0]["output_schema"].update(type="imaginary"),
    lambda m: m["tools"][0]["adapter"].update(handler="not_a_real_handler"),
])
def test_malformed_manifest_fails_startup_with_location(notes, change):
    folder, manifest = notes
    change(manifest)
    write_manifest(folder, manifest)
    with pytest.raises(ValueError, match="notes"):
        Catalog(folder.parent)


def test_no_default_offline_tool_required(notes):
    folder, manifest = notes
    del manifest["offline_tool"]
    write_manifest(folder, manifest)
    assert Catalog(folder.parent).discover("notes")[0]["name"] == "notes_search"


@pytest.mark.parametrize("schema,required", [
    (None, []), ({"type": "string"}, ["ids"]),
    ({"type": "array", "items": {"type": "integer"}}, ["ids"]),
    ({"type": "array", "items": {"type": "string"}}, []),
])
def test_object_ids_binding_requires_mandatory_string_array(notes, schema, required):
    folder, manifest = notes
    del manifest["offline_tool"]
    tool = manifest["tools"][0]
    tool["object_ids_field"] = "ids"
    tool["input_schema"]["required"].extend(required)
    if schema:
        tool["input_schema"]["properties"]["ids"] = schema
    write_manifest(folder, manifest)
    with pytest.raises(ValueError, match="object_ids_field"):
        Catalog(folder.parent)


def test_discovery_exposes_object_binding_only_if_configured(notes):
    folder, manifest = notes
    assert "object_ids_field" not in Catalog(folder.parent).discover("notes")[0]
    del manifest["offline_tool"]
    tool = manifest["tools"][0]
    tool["object_ids_field"] = "ids"
    tool["input_schema"]["properties"]["ids"] = {"type": "array", "items": {"type": "string"}}
    tool["input_schema"]["required"].append("ids")
    write_manifest(folder, manifest)
    assert Catalog(folder.parent).discover("notes")[0]["object_ids_field"] == "ids"


def test_notes_retrieval_returns_only_matching_local_evidence(notes):
    folder, _ = notes
    catalog = Catalog(folder.parent)
    chunks = catalog.retrieve("notes", "会议纪要", top_k=1)
    assert len(chunks) == 1
    assert set(chunks[0]) == {"source_id", "title", "snippet", "version"}
    assert "会议纪要" in chunks[0]["title"] + chunks[0]["snippet"]
    result = run(catalog, "notes", "notes_search", {"text": "会议纪要"})
    assert chunks[0] in result["evidence"]
    assert chunks[0]["snippet"] in result["data"]["summary"]


def test_notes_tool_reports_no_matching_evidence(notes):
    folder, _ = notes
    result = run(Catalog(folder.parent), "notes", "notes_search", {"text": "xyzzy"})
    assert result["evidence"] == []
    assert "未找到" in result["data"]["summary"]


def test_short_exact_query_matches_document_title(notes):
    folder, _ = notes
    assert Catalog(folder.parent).retrieve("notes", "会")[0]["source_id"] == "note-meeting"


def test_local_adapter_cannot_bypass_current_retrieval_grant(notes):
    folder, _ = notes
    def deny():
        raise Fault("PERMISSION_DENIED", "retrieval revoked", 403)

    with pytest.raises(Fault) as error:
        asyncio.run(Catalog(folder.parent).execute("notes", "notes_search", {"text": "会议"},
                                                  authorize_retrieval=deny))
    assert error.value.code == "PERMISSION_DENIED"


@pytest.mark.parametrize("args", [{}, {"text": 2}, {"text": "x", "url": "http://127.0.0.1"},
                                   {"text": ""}, {"text": "x" * 8193}])
def test_invalid_tool_inputs_denied_before_dispatch(notes, args):
    folder, _ = notes
    with pytest.raises(Fault) as error:
        run(Catalog(folder.parent), "notes", "notes_search", args)
    assert error.value.code == "SCHEMA_VALIDATION_FAILED"


def test_commit_write_cannot_run_even_if_published(notes):
    folder, manifest = notes
    manifest["tools"][0]["action_level"] = "commit_write"
    write_manifest(folder, manifest)
    with pytest.raises(Fault) as error:
        run(Catalog(folder.parent), "notes", "notes_search", {"text": "x"})
    assert error.value.code == "ACTION_NOT_ALLOWED"


def test_invalid_published_handler_output_is_rejected(notes):
    folder, manifest = notes
    (folder / "adapter.py").write_text("def bad(arguments, retrieve): return {'data': {}, 'evidence': []}", encoding="utf-8")
    manifest["tools"][0]["adapter"]["handler"] = "bad"
    write_manifest(folder, manifest)
    with pytest.raises(Fault) as error:
        run(Catalog(folder.parent), "notes", "notes_search", {"text": "x"})
    assert error.value.code == "CAPABILITY_OUTPUT_INVALID"


def test_strict_http_destination(notes):
    folder, manifest = notes
    manifest["tools"][0]["adapter"] = {"type": "http", "url": "http://127.0.0.1/notes", "method": "POST"}
    write_manifest(folder, manifest)
    with pytest.raises(ValueError, match="notes"):
        Catalog(folder.parent)


def test_http_tool_uses_exact_configured_target_and_rejects_redirect(notes):
    folder, manifest = notes
    manifest["tools"][0]["adapter"] = {"type": "http", "url": "https://example.test/notes", "method": "POST"}
    write_manifest(folder, manifest)
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(307, headers={"Location": "http://localhost/secret"})
    catalog = Catalog(folder.parent, http_transport=httpx.MockTransport(respond))
    with pytest.raises(Fault):
        run(catalog, "notes", "notes_search", {"text": "x"})
    assert [str(req.url) for req in requests] == ["https://example.test/notes"]
    assert json.loads(requests[0].content) == {"text": "x"}


@pytest.mark.parametrize("response,code,retryable", [
    (httpx.Response(429), "RATE_LIMITED", True),
    (httpx.Response(503), "UPSTREAM_UNAVAILABLE", True),
    (httpx.Response(200, content=b"invalid-json"), "UPSTREAM_INVALID_RESPONSE", False),
    (httpx.Response(200, json={"data": {}, "evidence": []}), "CAPABILITY_OUTPUT_INVALID", False),
])
def test_http_failure_protocol(notes, response, code, retryable):
    folder, manifest = notes
    manifest["tools"][0]["adapter"] = {"type": "http", "url": "https://example.test/notes", "method": "POST"}
    write_manifest(folder, manifest)
    catalog = Catalog(folder.parent, http_transport=httpx.MockTransport(lambda req: response))
    with pytest.raises(Fault) as error:
        run(catalog, "notes", "notes_search", {"text": "x"})
    assert (error.value.code, error.value.retryable) == (code, retryable)
