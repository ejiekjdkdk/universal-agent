# 离线发布评测与运行观测

从项目根目录运行固定金标。命令向标准输出写入 JSON，任一门禁失败或目录没有场景时返回非零退出码。

```powershell
python -m app.evaluations --scenarios scenarios
python -m app.evaluations --scenarios examples/durable/scenarios
python -m app.evaluations --scenarios examples/governance/scenarios --scene change_requests
```

## 金标和发布决策

`v1/<场景 ID>.json` 由平台维护。每条用例记录输入、测试对象范围、预期状态、能力调用顺序、证据 ID、允许的状态变体和安全约束。测试判断使用已审定的答案片段。运行报告记录各项判断是否通过，省略输入、用户身份、答案、原文和上游异常文本。

| 场景 | 正向判断 | 对照或拒绝判断 |
| --- | --- | --- |
| notes | 实际检索并返回指定原文 ID | 无匹配时明确提示，证据为空 |
| text | 实际统计字符及重复词 | 需要的工具、数值和输出 Schema 必须匹配 |
| project_brief | 真正运行三个持久化步骤，检查汇总内容及两个来源 | 工具顺序、依赖输出或证据变化会使相应用例失败 |
| change_requests | 在空的临时 SQLite 账本上建立待审批提案 | 关闭模拟写开关时拒绝，所有用例均检查累计回执数为零 |

每次评测还会执行固定安全探针。受控检索结果包含要求越权写入的文本，测试把其中的工具名交给真实网关并验证拒绝。其他探针覆盖跨租户任务访问、请求幂等、计划环路和超额预算。注入用例检验网关在规划器照做时仍能拒绝，不测量某一模型识别恶意提示的概率。

评测器只装载与平台批准的能力、动作、权限和 Python 源码 SHA-256 一致的绑定。哈希按 LF 换行计算。检查后的文件字节复制到新的临时目录，原目录中的缓存字节码和额外文件不会进入执行环境。只读示例使用真实适配器。唯一接受的写适配器为平台内置 mock_ledger，其独立测试账本不会收到审批决定。未知 Python 源码、空金标和缺少金标均使门禁失败。接入新场景需要平台独立评审代码与固定金标。该执行约束适用于评测器，部署时加载的 Python 场景始终需要代码评审。

只读 HTTP 工具的操作方金标把能力绑定记为 `["http", null, "<permission>", "read"]`，并在套件顶层提供 `http_fixtures` 数组。每条录制记录的形式如下。评测保留真实选路、请求构造、HTTP 状态处理与输出 Schema 校验，传输层直接返回已批准的响应字节。目标地址、方法或入参不匹配，以及录制记录未被任何用例使用，都会阻止发布。评测全过程不连接录制中列出的主机。

```json
{
  "capability_id": "lookup",
  "method": "POST",
  "url": "https://records.example/v1/query",
  "arguments": {"text": "sample"},
  "status_code": 200,
  "response": {"data": {"summary": "fixture result"}, "evidence": []}
}
```

报告包含 `passed`、`golden_version`、`planner_version`、场景版本、金标文件摘要、各用例结果及失败用例。指标给出路由正确率、需证据用例覆盖率、安全探针通过率、分母相关计数。无适用样本的比率为 null。全部固定用例均须通过。离线样例数量和标签适用于工程回归，真实模型质量与生产 SLA 需要独立样本和在线观测。

## 指标与受控追踪

三个只读端点均要求独立的管理员 Bearer 令牌。它们使用前缀 `/api/v1/agent-admin/observability`。

| 路径 | 内容 |
| --- | --- |
| `/metrics` | 最近 1000 个任务的完成率、部分完成率、分执行模式 P95 耗时、逻辑工具调用、失败尝试、租约恢复结果。审批等待取最近 1000 次有决定的记录。拒绝权限计数为累计值。首个 SSE 事件延迟来自当前 API 进程最近 1024 次流式聊天响应，从 ASGI 收到请求算至首次发送事件。 |
| `/alerts` | 最近 15 分钟内的权限拒绝数、待审批数、结果未知的模拟提交数，并检查无批准回执及同一步多次产生回执。待审批表示队列状态，其他安全异常须检查对应审批与审计记录。 |
| `/traces/{task_id}?page=0` | 供本地下载或转交采集器的 OTLP/HTTP JSON 请求体。每页取最多 128 个已记录事件、128 次工具调用。存在下一页时返回 `X-Next-Page`。各页共享相同根 span，合并时按 traceId/spanId 去重，终态后导出可得稳定快照。 |

导出包含真实任务与执行标识。步骤、审批和执行标识从同一任务的数据库记录确认。有限集合之外的错误码显示为 OTHER。模型标识只在与服务端配置一致时导出稳定摘要。请求与工具 span 的起止时间来自持久记录。生命周期事件按事件时间导出为瞬时 span。该端点提供本地采集数据，外部 Collector 和生产告警通知需另行配置。

### 模型 token 和成本

Chat Completions 返回非负整数 `usage.prompt_tokens` 与 `usage.completion_tokens` 时，服务记录本轮的 `model_usage`。若同时提供 `total_tokens`，其值必须等于两者之和。其他上游属性不会进入用量事件。可通过服务端环境变量 `AGENT_MODEL_INPUT_USD_PER_MILLION`、`AGENT_MODEL_OUTPUT_USD_PER_MILLION` 配置输入和输出各自的每百万 token 美元价格，或在模型设置中提供 `input_usd_per_million` 和 `output_usd_per_million`。接受 0 至 1,000,000 范围内的有限值。费率缺失或非法时仍记录有效 token，成本留空。

每轮估算额为输入 token × 输入费率 / 百万，加上输出 token × 输出费率 / 百万。按该轮调用时的配置保存估算额，费率变更后不会重估历史任务。估算未拆分缓存折扣或供应商阶梯账单。

| 字段 | 口径 |
| --- | --- |
| `model_round_count`、`usage_round_count`、`priced_round_count` | 最近 1000 个任务内实际发起的模型轮数、具有有效 token 的轮数、两种费率齐全的轮数 |
| `usage_round_coverage` | 有效用量轮数 / 发起轮数；无发起轮数时为 null |
| `observed_token_usage`、`observed_estimated_cost_usd` | 仅合计已观测的 token 或已计价轮次，无该类观测时为 null |
| `token_usage`、`estimated_cost_usd` | 仅在所有轮次分别具有有效用量或完整价格时提供全量值，否则为 null |
| `token_cost_per_task` | 全量估算额除以发起过模型调用的任务数，单位 USD/任务。缺少任何轮次的费率或用量时为 null |

OTLP 对有效观测导出数值属性。未知用量与成本在指标 JSON 中为 null，相应 OTLP 属性省略。

OTLP 请求体采用十六进制 traceId/spanId、纳秒整数字符串和数值枚举。[OpenTelemetry OTLP/HTTP JSON 规范](https://opentelemetry.io/docs/specs/otlp/#json-protobuf-encoding)
