# 个人工作台接口

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
| GET /api/v1/agent/tasks/{id}/events | SSE 事件流，通过 Last-Event-ID 从上次位置恢复 |
| POST /api/v1/agent/tasks/{id}/cancel | 尽力取消任务，以持久化终态为准 |

`business_id` 表示场景 ID。传入同一 `session_id` 可继续会话。请求体拒绝未经声明的字段。对象范围在服务端检查。使用新生成的 `Idempotency-Key` 标识一次提交；原样重试返回同一任务，同键不同正文返回 409。录入成功后，任务运行错误保存在任务的 `error` 字段。客户端同时检查 HTTP 状态和任务状态。

## 读取 Trace

Trace 的事件 ID 在任务内递增。前端可实时订阅，或稍后通过 JSON 列表查看。每次工具执行分配 `execution_id`，开始、输入摘要、完成和输出事件可据此关联。授权事件记录校验的工具或权限。文本参数只记录长度和哈希前缀，耗时以毫秒表示；最终回答和被引用的资料仍会保存在任务结果及其终态事件中。源文与问题可能含私人内容，请按个人资料保管数据库。

终态事件为 `task_completed`、`task_failed` 或 `task_cancelled`。流式接口按步骤推送，当前两种规划器在得到完整回答时发送一个 `message_delta`。恢复后使用任务结果覆盖展示，可避免重复追加。

## 服务间调用

三个固定入口为 `POST /internal/agent/discover`、`/execute`、`/retrieve`（后两者同样位于 `/internal/agent` 下）。这些端点同时需要单独配置的 `AGENT_SERVICE_TOKEN` 和属于运行中任务的短期 `run_context_id`。公开 API 不返回此 ID。默认本地规划器直接调用同一网关。接入外部编排服务时需在受信后端传递运行上下文。模型端点仅需工具调用能力。

## 存储与运行边界

服务以单进程运行。默认 SQLite 文件为 `var/agent.db`，可通过 `AGENT_DATABASE` 指定。数据库持久化任务、事件、会话、执行摘要和审计；重启会将中断任务标记为失败，历史仍可查询。每次任务的工具数、模型轮数、工具并发与总耗时有上限。只读工具遇到符合条件的暂时故障可重试一次，取消或超时不能撤回已发出的远程请求。

离线模式可用来开发和验收扩展。接入企业环境时，由项目提供 SSO 身份、资源授权与服务端凭据管理。
