# 个人工作台接口

V2 另有独立身份审批、模拟回执、发布管理、指标与 OTLP 导出接口。[管理与审批接口说明](v2/governance.md)

本机运行服务后，访问 `/openapi.json` 可获得机器可读的 API 描述。所有外部业务请求均携带 `Authorization: Bearer <本地访问令牌>`。默认个人模式为每次启动生成临时令牌，浏览器从同源回环入口获取；显式配置 `AGENT_ACCESS_TOKEN` 时改为手工登录。默认启动命令绑定 127.0.0.1，关闭代理头信任。部署多用户服务需要独立身份认证和授权配置。

| 方法与路径 | 行为 |
| --- | --- |
| GET /api/v1/agent/scenarios | 已授权场景清单与模型配置状态 |
| POST /api/v1/agent/sessions | 创建绑定身份和场景的会话 |
| GET /api/v1/agent/sessions/{id} | 查询会话及授权范围 |
| POST /api/v1/agent/chat | 运行任务，支持 offline/openai 和 blocking/streaming |
| GET /api/v1/agent/tasks | 当前身份的最近任务，支持 business_id 过滤 |
| GET /api/v1/agent/tasks/{id} | 任务状态、回答、工具调用、证据、错误和审计 ID |
| GET /api/v1/agent/tasks/{id}/trace | 持久化的有序事件，含时间和事件 ID |
| GET /api/v1/agent/tasks/{id}/plan | 持久计划摘要、依赖和版本快照 |
| GET /api/v1/agent/tasks/{id}/steps | 持久计划各步状态、尝试次数与错误代码 |
| GET /api/v1/agent/tasks/{id}/events | SSE 事件流，通过 Last-Event-ID 从上次位置恢复 |
| POST /api/v1/agent/tasks/{id}/cancel | 尽力取消任务，以持久化终态为准 |

`business_id` 表示场景 ID。传入同一 `session_id` 可继续会话。请求体拒绝未经声明的字段。对象范围在服务端检查。使用新生成的 `Idempotency-Key` 标识一次提交；原样重试返回同一任务，同键不同正文返回 409。录入成功后，任务运行错误保存在任务的 `error` 字段。客户端同时检查 HTTP 状态和任务状态。

## 读取 Trace

Trace 的事件 ID 在任务内递增。前端可实时订阅，或稍后通过 JSON 列表查看。每次工具执行分配 `execution_id`，开始、输入摘要、完成和输出事件可据此关联。授权事件记录校验的工具或权限。文本参数只记录长度和哈希前缀，耗时以毫秒表示；最终回答和被引用的资料仍会保存在任务结果及其终态事件中。源文与问题可能含私人内容，请按个人资料保管数据库。

终态事件为 `task_completed`、`task_failed` 或 `task_cancelled`。流式接口按步骤推送，当前两种规划器在得到完整回答时发送一个 `message_delta`。恢复后使用任务结果覆盖展示，可避免重复追加。

## 服务间调用

三个固定入口为 `POST /internal/agent/discover`、`/execute`、`/retrieve`（后两者同样位于 `/internal/agent` 下）。这些端点同时需要单独配置的 `AGENT_SERVICE_TOKEN` 和属于运行中任务的短期 `run_context_id`。公开 API 不返回此 ID。默认本地规划器直接调用同一网关。接入外部编排服务时需在受信后端传递运行上下文。模型端点仅需工具调用能力。

## 存储与运行边界

默认 SQLite 文件为 `var/agent.db`，可通过 `AGENT_DATABASE` 指定。原有 reactive 任务由一个 API 进程运行，重启时中断任务记为失败，历史仍可查询。显式选择 `execution_mode=durable_plan` 的任务保存计划和步骤，可由嵌入式或独立 Worker 恢复。JSON 提交返回 202 和查询地址，流式提交返回 200 SSE。每次任务的调用、并发与耗时均受限，取消或超时不能撤回已发出的远程请求。[可恢复计划协议](v2/durable-plans.md)

离线模式可用来开发和验收扩展。接入企业环境时，由项目提供 SSO 身份、资源授权与服务端凭据管理。
# V3 P1 技能扩展

业务接口沿用 `/api/v1/agent` 和原 Bearer 身份。交互请求可使用 `skill_id` 及 `mode: "mock"`。对已绑定技能的场景，只有一个可用技能时可由服务端选择，多个时须明确指定。技能绑定场景本阶段使用 reactive 只读运行。直接返回 SSE 的旧客户端仍可使用 `response_mode: "streaming"`。

| 接口或事件 | 约定 |
| --- | --- |
| GET `/skills?business_id=...` | 返回 `skills` 元数据列表。只列出已发布、当前身份具备所需能力的技能。 |
| GET `/capabilities?business_id=...` | 返回供展示的已注册场景工具。通用检索入口的权限由服务端单独核对。 |
| POST `/chat` | 请求可携带 `context.object_ids`。技能版本由服务端固定。终态除了回答和证据，还可从 `result.limitations`、`result.unresolved_items` 获取局限和待确认事项。 |
| GET `/tasks/{id}/events` | `skill_selected` 记录技能名、版本和发布快照；工具调用事件通过执行 ID 与结果关联。用 `Last-Event-ID` 续读。 |
| 错误 | `SKILL_REQUIRED` 表示需要选择技能；`SKILL_UNAVAILABLE` 表示技能不可用或依赖未授权；`MISSING_EXECUTION_EVIDENCE` 表示缺少实际执行记录；`SENSITIVE_OUTPUT` 表示输出含凭据并已中止。 |

导入、绑定、撤销通过受信任的本机 CLI 完成。可执行的 PowerShell 示例见[双项目运行指南](../examples/v3/README.md)。
