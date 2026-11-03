"""用固定虚构输入比较上下文体积；结果不是模型Token或性能评测。"""
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.context_compiler import ContextItem, compile_context


def main():
    scope = ("demo-tenant", "demo-org", "demo-user", "demo-business", "demo-task")
    evidence = [{"source_id": "demo-a", "version": "1", "data_as_of": timestamp,
                 "source_ref": "demo://collection/a", "citation": citation}
                for timestamp, citation in [("2026-11-03T00:00:00Z", "[E1]"), ("2026-11-03T01:00:00Z", "[E2]")]]
    result = {"data": {"summary": "两次虚构观测", "details": "x" * 30000}, "evidence": evidence}
    original = json.dumps(result, ensure_ascii=False, sort_keys=True)
    summary = {"data": {"summary": result["data"]["summary"]}, "evidence": evidence,
               "context_ref": "demo-result", "limitations": ["省略可按引用读取的明细"]}
    call = {"role": "assistant", "content": None, "tool_calls": [{"id": "one", "type": "function",
            "function": {"name": "execute_capability", "arguments": "{}"}}]}
    messages = [("policy", "system_policy", "trusted_config", {"role": "system", "content": "仅访问已授权对象"}, True),
                ("goal", "user_message", "user_input", {"role": "user", "content": "比较两次观测"}, True),
                ("history", "history", "untrusted_data", {"role": "user", "content": "旧历史" * 2000}, False),
                ("call", "tool_call", "untrusted_data", call, True),
                ("demo-result", "tool_result", "untrusted_data", {"role": "tool", "tool_call_id": "one", "content": original}, True)]
    items = []
    for item_id, kind, trust, message, pinned in messages:
        compact = None
        if item_id == "demo-result":
            compact = json.dumps({**message, "content": json.dumps(summary, ensure_ascii=False)}, ensure_ascii=False)
        items.append(ContextItem(item_id, scope, "demo-snapshot", kind, trust,
                                 json.dumps(message, ensure_ascii=False), pinned, 100, compact))
    before = compile_context(items, scope=scope, snapshot_id="demo-snapshot", input_budget=100000, tool_tokens=256, now=0)
    after = compile_context(items, scope=scope, snapshot_id="demo-snapshot", input_budget=8000, tool_tokens=256, now=0)
    compiled_result = json.loads(json.loads(after.messages_json[-1])["content"])
    assert compiled_result["evidence"] == evidence
    assert json.dumps(result, ensure_ascii=False, sort_keys=True) == original
    assert after.summary_refs == ("demo-result",) and after.omitted_item_ids == ("history",)
    report = {"status": "PASS", "measurement": "synthetic_utf8_bytes_plus_envelope",
              "before_estimate": before.token_estimate, "after_estimate": after.token_estimate,
              "omitted_item_ids": after.omitted_item_ids, "summary_refs": after.summary_refs,
              "evidence_before": len(evidence), "evidence_after": len(compiled_result["evidence"]),
              "source_result_sha256": hashlib.sha256(original.encode("utf-8")).hexdigest(),
              "source_result_unchanged": True, "real_model_tokens": "NOT_RUN", "latency_comparison": "NOT_RUN",
              "limitations": after.limitations}
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    configure_stdout = getattr(sys.stdout, "reconfigure", None)
    if configure_stdout:
        configure_stdout(encoding="utf-8")
    main()
