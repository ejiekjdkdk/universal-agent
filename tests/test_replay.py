"""本地回放必须重走实际授权且不能回退到真实网络或脚本。"""
import copy
import importlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import httpx
import pytest


KEY = b"fictional-replay-signing-key-123456789"


def replay_module():
    assert importlib.util.find_spec("app.replay") is not None, "P3回放尚未实现"
    return importlib.import_module("app.replay")


def case():
    call = {"id": "read-1", "type": "function", "function": {"name": "execute_capability",
            "arguments": json.dumps({"name": "lookup", "arguments": {"text": "sample"}})}}
    return {
        "schema_version": "3.1", "case_id": "sample", "version": "baseline", "fixture_policy": "synthetic",
        "release_snapshot_id": "fixture-1", "model_profile_version": "mock-1", "time": 1900000000,
        "message": "sample", "permissions": ["record.read"], "object_ids": ["a"], "redactions": [],
        "expected_status": "completed", "tools": [{
            "name": "lookup", "permission": "record.read", "action_level": "read",
            "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"], "additionalProperties": False},
            "output_schema": {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"], "additionalProperties": False},
            "recordings": [{"arguments": {"text": "sample"}, "output": {
                "data": {"summary": "虚构结果"}, "evidence": [{"source_id": "fixture-a", "title": "虚构记录",
                "snippet": "虚构结果", "version": "1", "object_id": "a", "source_ref": "fixture://a"}]}}],
        }],
        "responses": [
            {"status_code": 200, "body": {"choices": [{"message": {"role": "assistant", "tool_calls": [call]}, "finish_reason": "tool_calls"}]}},
            {"status_code": 200, "body": {"choices": [{"message": {"role": "assistant", "content": json.dumps({"answer": "虚构结果 [E1]", "limitations": [], "unresolved_items": []})}, "finish_reason": "stop"}]}},
        ],
    }


def test_record_and_replay_use_same_structural_result_and_real_gateway(tmp_path, monkeypatch):
    module = replay_module()
    async def deny_network(*args, **kwargs):
        raise AssertionError("回放尝试了真实网络")
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", deny_network)
    bundle = tmp_path / "case.zip"
    recorded = module.record_case(case(), bundle, KEY)
    assert recorded["status"] == "PASS"
    first = module.run_replay(bundle, KEY)
    second = module.run_replay(bundle, KEY)
    assert first["status"] == second["status"] == "PASS"
    assert first["result"] == second["result"]
    assert first["result"]["status"] == "completed"
    assert len(first["result"]["tool_calls"]) == 1 and first["result"]["permissions"] == ["record.read"]


def test_failed_case_is_reproducible_without_calling_missing_recording(tmp_path):
    module = replay_module()
    value = case()
    value["tools"][0]["recordings"][0]["arguments"]["text"] = "other"
    value["expected_status"] = "failed"
    path = tmp_path / "failure.zip"
    assert module.record_case(value, path, KEY)["status"] == "PASS"
    result = module.run_replay(path, KEY)
    assert result["status"] == "PASS" and result["result"]["status"] == "failed"


def test_redaction_happens_before_recording_and_bundle_has_no_secret(tmp_path):
    module = replay_module()
    value = case()
    secret = "fictional-sensitive-contract-987"
    value["redactions"] = [secret]
    value["tools"][0]["recordings"][0]["output"]["data"]["summary"] = secret
    value["tools"][0]["recordings"][0]["output"]["evidence"][0]["snippet"] = secret
    path = tmp_path / "redacted.zip"
    module.record_case(value, path, KEY)
    with zipfile.ZipFile(path) as archive:
        raw = b"".join(archive.read(name) for name in archive.namelist())
    assert secret.encode() not in raw and KEY not in raw
    assert module.run_replay(path, KEY)["status"] == "PASS"


@pytest.mark.parametrize("change", ["write", "adapter", "script", "schema_ref"])
def test_untrusted_case_cannot_enable_execution_paths(tmp_path, change):
    module = replay_module()
    value = case()
    if change == "write":
        value["tools"][0]["action_level"] = "commit_write"
    elif change == "adapter":
        value["tools"][0]["adapter"] = {"type": "http", "url": "https://real.invalid"}
    elif change == "schema_ref":
        value["tools"][0]["input_schema"] = {"$ref": "file:///secret"}
    else:
        value["script"] = "arbitrary.py"
    with pytest.raises((ValueError, RuntimeError)):
        module.record_case(value, tmp_path / "rejected.zip", KEY)
    assert not (tmp_path / "rejected.zip").exists()


@pytest.mark.parametrize("change", ["content", "signature", "extra_path", "duplicate"])
def test_bundle_is_verified_before_execution(tmp_path, change):
    module = replay_module()
    source, changed = tmp_path / "good.zip", tmp_path / "bad.zip"
    module.record_case(case(), source, KEY)
    with zipfile.ZipFile(source) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    if change == "content":
        files["case.json"] = files["case.json"].replace(b"baseline", b"tampered")
    if change == "signature":
        manifest = json.loads(files["manifest.json"])
        manifest["signature"] = "0" * 64
        files["manifest.json"] = json.dumps(manifest).encode()
    if change == "extra_path":
        files["../escape.json"] = b"{}"
    with zipfile.ZipFile(changed, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
        if change == "duplicate":
            with pytest.warns(UserWarning):
                archive.writestr("case.json", files["case.json"])
    with pytest.raises(ValueError):
        module.run_replay(changed, KEY)


def test_differences_include_contract_and_block_permission_expansion(tmp_path):
    module = replay_module()
    baseline, candidate = tmp_path / "a.zip", tmp_path / "b.zip"
    module.record_case(case(), baseline, KEY)
    value = case()
    value["version"] = "candidate"
    value["permissions"].append("extra.read")
    module.record_case(value, candidate, KEY)
    report = module.compare_bundles(baseline, candidate, KEY)
    assert report["status"] == "FAIL" and report["permission_delta"]["added"] == ["extra.read"]
    assert {"case_id", "baseline_version", "candidate_version", "input_fixture_digest", "release_snapshot_id",
            "model_profile_version", "differences", "state_delta", "evidence_delta", "schema_delta",
            "usage_delta", "latency_delta"} <= set(report)
    assert report["usage_delta"]["cost_difference_usd"] is None


def test_different_inputs_are_not_presented_as_improvement(tmp_path):
    module = replay_module()
    a, b = tmp_path / "a.zip", tmp_path / "b.zip"
    module.record_case(case(), a, KEY)
    value = case()
    value["message"] = "other input"
    module.record_case(value, b, KEY)
    assert module.compare_bundles(a, b, KEY)["status"] == "INCOMPARABLE"


def test_same_bundle_is_reproducible_but_does_not_claim_real_model_quality(tmp_path):
    module = replay_module()
    path = tmp_path / "same.zip"
    module.record_case(case(), path, KEY)
    result = module.compare_bundles(path, path, KEY)
    assert result["status"] == "PASS" and result["real_model_validation"] == "NOT_RUN"


def test_permission_denied_is_exercised_by_gateway(tmp_path):
    module = replay_module()
    value = case()
    value["permissions"] = []
    value["expected_status"] = "failed"
    path = tmp_path / "denied.zip"
    module.record_case(value, path, KEY)
    result = module.run_replay(path, KEY)["result"]
    assert result["status"] == "failed" and result["error"]["code"] == "PERMISSION_DENIED"


def test_signing_key_engine_version_size_and_overwrite_are_guarded(tmp_path, monkeypatch):
    module = replay_module()
    path = tmp_path / "case.zip"
    module.record_case(case(), path, KEY)
    with pytest.raises(ValueError):
        module.run_replay(path, b"wrong-key" * 8)
    with pytest.raises(FileExistsError):
        module.record_case(case(), path, KEY)
    monkeypatch.setattr(module, "_engine_digest", lambda: "changed")
    with pytest.raises(ValueError):
        module.run_replay(path, KEY)
    path.write_bytes(b"0" * (module.MAX_BYTES + 1))
    with pytest.raises(ValueError):
        module.inspect_bundle(path, KEY)


@pytest.mark.parametrize("change", ["schema", "missing_evidence"])
def test_diff_blocks_contract_and_evidence_regressions(tmp_path, change):
    module = replay_module()
    baseline, candidate = tmp_path / "a.zip", tmp_path / "b.zip"
    module.record_case(case(), baseline, KEY)
    value = case()
    if change == "schema":
        value["responses"][1]["body"]["choices"][0]["message"]["content"] = '{}'
        value["expected_status"] = "failed"
    else:
        # 同一输入与工具录制，候选模型跳过检索，因此最终证据丢失。
        value["responses"] = value["responses"][1:]
    module.record_case(value, candidate, KEY)
    report = module.compare_bundles(baseline, candidate, KEY)
    assert report["status"] == "FAIL"
    if change == "schema":
        assert report["schema_delta"]["candidate_valid"] is False
    else:
        assert report["evidence_delta"]["lost_digests"]


def test_redaction_must_not_silently_merge_keys():
    module = replay_module()
    with pytest.raises(ValueError):
        module._redact({"secret-one": 1, "secret-two": 2}, ["secret-one", "secret-two"])


def test_cli_record_inspect_run_compare(tmp_path):
    fixture, bundle = tmp_path / "case.json", tmp_path / "case.zip"
    fixture.write_text(json.dumps(case()), encoding="utf-8")
    environment = {**os.environ, "REPLAY_TEST_KEY": KEY.decode(), "PYTHONIOENCODING": "utf-8"}
    script = Path(__file__).resolve().parents[1] / "scripts" / "replay.py"
    commands = [
        ["record", "--case", str(fixture), "--out", str(bundle)],
        ["inspect", "--bundle", str(bundle)], ["run", "--bundle", str(bundle)],
        ["compare", "--baseline", str(bundle), "--candidate", str(bundle)],
    ]
    for args in commands:
        result = subprocess.run([sys.executable, str(script), *args, "--key-env", "REPLAY_TEST_KEY"],
                                env=environment, capture_output=True, text=True, encoding="utf-8", timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
        assert json.loads(result.stdout)["status"] == "PASS"
        assert KEY.decode() not in result.stdout + result.stderr
