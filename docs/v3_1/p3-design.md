# V3.1 P3 实施设计

基线：main@82b1a7337ff71b6bd5ffc11ec3daca7144d0d669。依据原设计 §6、§8、V31-036～043，沿用 V3 唯一 Runtime/Gateway/Store/Worker。用户已确认只显式录制本地测试，不自动录制日常任务；验收只用本机模拟模型 HTTP 与虚构数据，真实商业模型为 NOT_RUN。用户授权评估无未决问题后直接开发。

## 模型路由

新增 app/model_router.py，策略来自可信 model_settings.provider_policy 或 AGENT_PROVIDER_POLICY 指向的有界 JSON；没有配置时完全保留现有模式。策略包含 version、profiles（按 ID 的映射）、routes（按 business_id），未知字段拒绝。不增加数据库表或公开 API。

画像字段：provider_id、model_ref、base_url、credential_env、enabled、profile_version、verified_at/expires_at（Unix 秒）、verification_kind（mock/operator_verified）、evidence_digest、supports_tool_calling/structured_output/streaming/parallel_tool_calls/usage_reporting（true/false/unknown）、context_window_tokens、max_output_tokens、data_handling_class（允许的数据类别列表）、region、retention_class、input/output_usd_per_million。未知或过期能力不作为支持；mock 画像只允许回环端点，不能用它声称已验证商业模型。容量和价格为受信配置与对应证据，不从型号推断。

路由字段：allowed_providers（有序选择，最多 8）、data_class、allowed_regions、allowed_retention_classes、result_max_age_s（1～3600）、stream、max_attempts（1～8，任务总模型请求最多 32）、max_total_tokens、max_cost_usd、timeout_s。按数据类别/区域、启用状态、验证有效期和所需能力筛选。严格结构化输出为 P3 必需能力，工具协议必需；stream 为 true 时必须支持流。保留顺序表达操作者已经评测的质量/成本优先级；一次任务跳过已经失败的候选，不建立新的分布式健康存储。

首次使用冻结策略摘要和画像，后续每次发送前重新加载可信配置，检查未改变、未吊销、凭据仍一致及当前任务权限。策略/凭据改变拒绝继续旧任务。选定模型后才设置 ContextSession 的窗口与输出预留，失败切换到较小模型必须重新编译；仍采用 UTF-8 字节加封装余量的保守估算，不称为真实 tokenizer。

在既有 _model_loop 内只重试当前模型请求，不重启整个循环。429/503/408、连接错误、超时和不完整流可以有界切换；无效最终结构、权限或预算失败不可用切换掩盖。保持调用顺序，完整收到并验证后才执行工具。模型换了 call_id 但请求同一已成功工具时，按入口、能力及规范化参数复用原结果，重新检查来源与结果引用，保留原 ToolExecution，不产生第二次业务执行。无法确定的工具结果直接沿用原失败/未知语义，不调用模型猜测恢复。durable_plan 不新增模型阶段。

P3 请求使用 Chat Completions json_schema strict 格式和固定的 answer/limitations/unresolved_items 最终结构，仍在本地验证；拒绝 refusal、截断、重复 JSON 键、非唯一 choice、非法参数及能力不允许的并行调用。max_completion_tokens 与配置输出上限一致；stream 模式有界累计 SSE delta，必须收到有效 finish_reason 和 DONE；usage 单独统计。模型流不直接传入旧网页 SSE，旧网页仍在完成验证后发布一次 message_delta。

每次请求前按输入估算+最大输出预留 Token/成本；失败或缺失 usage 不退回预留。可信 usage 不足以覆盖未知请求时不能声称准确账单，摘要区分 observed 和 reserved。原 model_usage 只在本轮无未知尝试时记录已知成功用量；详细尝试进入 Store.audit，不新增 SSE 枚举或完整 Prompt。不同 Provider 使用各自价格。

## 本地 Replay

新增 app/replay.py 和 scripts/replay.py。命令 record / inspect / run / compare，只处理显式给定的本地虚构/已脱敏 JSON 测试用例，不读取日常任务数据库，不创建自动录制开关。候选模型在本轮通过两套可控 HTTP 响应表示；真实商业模型的 comparative 联调为 NOT_RUN，不伪装成确定性商业模型效果。

用例描述固定业务、权限/对象、工具 Schema、精确参数对应的录制输出、消息与模型 HTTP 响应序列及预期状态。禁止工件提供 Python 代码、真实适配器地址或凭据；运行器只构建内置 Recorded HTTP Adapter，通过实际 Catalog/Gateway/Runtime/Store 走原授权、解析和任务结算。HTTP 和模型 transport 均强制 MockTransport，无命中即拒绝，不回退真实网络；不加载包中脚本，不执行写工具。

record 先对输入显式脱敏（配置字面敏感值、已知凭据模式），再运行脱敏用例，记录解析结果/状态/权限/证据/Schema/usage/延迟和请求摘要；不导出原始未脱敏输入或服务凭据。用户自定义夹具必须人工检查；自动规则不能保证任意秘密均识别。脱敏造成 Schema 或语义不匹配时明确失败，不声称恢复原文。固定时间与录制响应，归一化任务随机 ID；结构结果可重复，墙钟延迟只作为测量值不参加确定性比较。

固定 ZIP 内容为 case.json、recording.json、manifest.json，最多 4 MiB；路径、重复名称、链接、压缩比与内容总量在内存有界核验，不解压到任意路径。清单包含版本、SHA256 和 HMAC；密钥来自操作者显式环境引用（至少 32 字节），不接受包内自带信任。工件中的版本仅用于标识/拒绝不支持版本，不加载旧任意代码。run 核对当前实现版本和固定夹具后重新运行实际循环；失败结果同样可重复复现。

compare 使用相同输入/工具夹具的 baseline 与 candidate 录制包。报告含 case_id、baseline/candidate_version、input_fixture_digest、release_snapshot_id、model_profile_version、differences、permission_delta、state_delta、evidence_delta、schema_delta、usage_delta、latency_delta 和 PASS/FAIL/INCOMPARABLE。输入不一致不能比较；权限扩大、关键证据丢失、终态回归、Schema 破坏或外部写入意图为 FAIL；文本不同不单独宣称质量变差或变好。不存在真实发布动作。

## 验证与回退

不新增依赖；中文注释和直观控制流；公开方法签名、默认 OpenAPI 和数据库契约快照继续通过。关闭 provider_policy 恢复旧模型路径，Replay CLI 不影响服务。先编写反例，再实现并验证：未知能力/过期/数据类别/区域拒绝；429/503/timeout/断流；小窗口切换；新 call_id 重复工具；撤权；预算未知消耗；并行与取消；工件篡改/越界/秘密；无网络回放；固定夹具差分；旧 538 项测试回归。实际本机 HTTP 服务单列验证，不与 MockTransport 混称。真实模型、真实企业写入、生产并发或分布式一律不在本轮验收。

协议参考（2026-11-04 实际读取）：[Chat Completions](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)、[流事件](https://developers.openai.com/api/reference/resources/chat/subresources/completions/streaming-events)。文档用于协议字段；各服务商是否实现须单独验证。

## 可实施性复核裁定

数据类别为可信操作者对整个业务全部出站内容（用户输入、历史、技能、工具结果）的保守上界，不依赖模型自行分类；profile.retention_class 必须属于 route.allowed_retention_classes。该配置不能证明第三方实际保存行为，真实部署另验。

任务结果固定本次观察时点；每次模型调用/故障复用前检查结果年龄不超过 result_max_age_s、授权/句柄未过期、可信 ReadCache freshness 配置未改变。任一失败即结束，不重执行以刷新结果；不承诺工具外部数据实时不变。
