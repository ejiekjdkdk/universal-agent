# V3.1 P3 验收记录

基线 main@82b1a7337ff71b6bd5ffc11ec3daca7144d0d669；实施分支 codex/v31-p3。用户确认仅显式本地测试 Replay、本地模拟模型 HTTP 与虚构数据；真实商业模型 NOT_RUN。分支交付时 main 尚未合并本轮；后续 main 集成与重新回归见 [P3 集成记录](../audits/v31-p3-integration.md)。

## 自动化覆盖

| 编号 | 验证点 | 主要测试 |
|---|---|---|
| V31-036 | 未知、禁用、过期画像不发送请求 | test_model_router.py::test_incompatible_profiles_never_receive_input |
| V31-037 | 严格输出能力门禁、strict请求、拒绝坏结构 | test_strict_request_and_known_usage、test_invalid_protocol_does_not_trigger_fallback |
| V31-038 | 类别/区域/保存策略、撤权和配置改变拒绝 | test_incompatible_profiles_never_receive_input、test_changed_policy_and_revoked_credentials_reject_before_send、test_fallback_cannot_bypass_authority_or_input_budget |
| V31-039 | 切换后新call_id不重执行；断流不执行半截工具 | test_fallback_new_call_id_reuses_completed_execution、test_provider_http.py三项 |
| V31-040 | 实际Runtime/Store/Gateway重复回放、失败用例复现 | test_record_and_replay_use_same_structural_result_and_real_gateway、test_failed_case_is_reproducible_without_calling_missing_recording |
| V31-041 | 无真实网络回退、只读、无任意适配器/脚本/引用 | test_untrusted_case_cannot_enable_execution_paths、test_record_and_replay_use_same_structural_result_and_real_gateway |
| V31-042 | 先脱敏、HMAC/摘要/路径/版本/大小核验 | test_redaction_happens_before_recording_and_bundle_has_no_secret、test_bundle_is_verified_before_execution、test_signing_key_engine_version_size_and_overwrite_are_guarded、test_redaction_must_not_silently_merge_keys |
| V31-043 | 契约/终态/权限/证据/用量/延迟差分与不可比 | test_differences_include_contract_and_block_permission_expansion、test_diff_blocks_contract_and_evidence_regressions、test_different_inputs_are_not_presented_as_improvement、test_cli_record_inspect_run_compare |

初始专项51 passed，16.86秒：22路由、6集成、20Replay、3真实回环HTTP。独立审查发现5类问题，复现并修复后新增8项覆盖，详见 [独立审查与修复](../audits/v31-p3-review.md)。

最终全量（2026-11-04，审计修复后）：`python -m pytest -q --tb=short`，**597 passed、12 subtests passed，220.59秒**，退出码0。最终日志为共享work/v31-p3-final-full.log。覆盖旧538项和本轮59项；V31-036～043在本机限定范围内通过。没有宣称真实商业模型、生产部署或任意业务语义已验收。

新增 app/model_router.py、app/replay.py 的 `mypy --follow-imports=skip --ignore-missing-imports --check-untyped-defs` 通过；compileall、pip check、static/app.js与static/review.js的node --check及git diff --check通过。不代表全库mypy无错误。

日志位于本机共享目录 `C:/Users/Lenovo/Documents/Codex/2026-11-03/universal-agent-agent-git-windows-trace/work/`，文件前缀v31-p3。源码包含完整自动化测试，日志不包含真实凭据或业务数据。

## 已确认边界

- 画像、区域、保存策略与价格依赖可信操作者配置；不是第三方能力自动认证。业务类别必须覆盖全部出站数据。商业模型实际支持、账单、效果、合规驻留均未验收。
- 本轮真实HTTP是本机模拟器；Replay本身强制MockTransport。二者不等于真实企业工具/商业模型接入。
- HMAC是本地信任；脱敏规则不能发现任意秘密。优先虚构夹具，人工复核导出包；不是通用数据防泄漏保证。
- 确定性回放要求执行代码摘要一致；比较使用固定输入与工具录制，文本变化不代表质量提升。全量历史数据自动录制、真实写工具回放、复杂Schema、自动执行技能脚本均不支持。
- Replay通过独立同步CLI固定时间，不可嵌入在线API进程。失败/未知usage保守占用预算，成本未知时不计算精确节省。
- 沿用SQLite单机Store/Worker，没有PostgreSQL、分布式运行或多模型自主专业Agent生产验证；未知副作用仍由既有恢复协议处理。
- 新功能默认不启用；移除provider_policy即可恢复旧模型路径，无数据库迁移。既有技能发布门禁/MCP边界未放宽。

自动测试是已覆盖场景的证据，不能证明任意业务语义正确或绝对无漏洞。
