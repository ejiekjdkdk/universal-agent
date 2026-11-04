# V3.1 P3 独立审查与修复

审查范围：main基线82b1a733至a3c3e729，全分支只读；独立审查者没有修改源码或重复运行全量。第一次全量589 passed、12 subtests passed、284.50秒，但审查随后确认下列反例，不能用已有测试通过否认问题。

| 级别 | 问题和复现 | 修复 | 回归测试（tests/test_p3_audit.py） |
|---|---|---|---|
| P1 | 新画像credential_env密钥未进入原Gateway防线，工具summary可含密钥并进入下一次模型请求；当前模型也可回显另一个候选密钥 | Router冻结全部画像凭据交给任务Gateway；工具入库、消息/工具Schema发送前、全部候选响应使用同一嵌套JSON凭据检查 | test_custom_profile_secret_cannot_enter_tool_store_or_model、test_reply_cannot_echo_another_candidate_credential、test_tool_schema_cannot_send_a_profile_credential |
| P2 | 工具响应带finish_reason=stop仍实际执行工具 | 要求非空工具调用对应tool_calls、最终结果对应stop，矛盾直接失败 | test_finish_reason_must_match_payload_before_execution |
| P2 | 已发送请求取消后没有model.attempt记录，可能计费请求被遗漏 | 取消记录usage_known=false与保留预留，原样重新抛出，不切换候选 | test_cancelled_attempt_preserves_unknown_usage_audit |
| P2 | 同一Replay工件受宿主AGENT_ADMIN_TOKEN/SERVICE_TOKEN影响 | 录制前仍基于原环境脱敏；执行阶段仅保留列举的系统/临时目录变量，清空服务配置与凭据 | test_replay_ignores_unrelated_live_service_credentials |
| P2 | 执行摘要只覆盖七文件，遗漏Schema等实际依赖 | 覆盖app下全部Python、Windows依赖锁、实际安装分发版本、Python版本和系统类别 | test_engine_digest_covers_execution_dependencies |

失败证据：首次6项中5项失败，1项旧路径已拒绝；取消新增反例单项失败；工具Schema夹带密钥反例单项失败。修复后8项通过8.83秒；此前58项P3专项通过18.81秒。最后全量结果见 [P3验收](../v3_1/p3-acceptance.md)。所有复现仅使用虚构密钥与数据。

## 采纳的裁定和影响

1. 业务类别覆盖全部出站数据，并以保存策略允许列表筛选。结果年龄/freshness变化直接拒绝，避免为恢复而重执行；代价是过期任务必须显式重新发起。
2. Replay仅支持有界基础Schema，不接受正则/外部引用。沿用Catalog对象additionalProperties:false门禁；代价是复杂项目需准备独立安全夹具。
3. 精确回放采用完整执行环境指纹，额外安装或升级依赖也会拒绝旧包；代价是应在固定虚拟环境运行。可以重新录制候选并差分，不能把不同环境误报为同一执行器。
4. 取消审计不改变取消、工具释放和预算语义；密钥检查遇到已知凭据即拒绝，包含用户输入或工具描述中的意外凭据，不进行自动发送。

## 明确未判定的范围

- 商业模型互通、真实质量/账单：用户限定本地模拟验收，NOT_RUN。
- 真实企业写入、生产分布式压力：未执行，不从本机回归推断生产可靠性。
- 任意未知业务秘密的自动识别：优先虚构、人工脱敏；修复的是系统明确掌握的模型凭据，非通用秘密识别保证。
- 精确Tokenizer与第三方地域/保存承诺：使用保守估算与可信操作者画像，未独立认证外部服务。

本次未遗留已确认的P1/P2发现，也没有以风格项替代安全修复；结论仍受上述范围限制。独立审查针对修复前head，修复由作者以失败反例转绿及最终全量验收，没有冒称二次独立审查。
