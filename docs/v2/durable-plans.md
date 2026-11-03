# 运行可恢复的只读计划

P1 提供本机 SQLite 调度器、只读场景模板、独立 Worker 和持久化步骤。已有场景仍可直接发起单次任务。以 Windows 为例，在仓库根目录打开 PowerShell，使用已安装项目依赖的 Python。

```powershell
$env:AGENT_SCENARIOS = (Resolve-Path .\examples\durable\scenarios).Path
$env:AGENT_DATABASE = Join-Path (Get-Location) 'var\brief.db'
python run.py
```

打开 <http://127.0.0.1:8765>，选择「项目简报」场景、持久计划执行方式，使用示例提问。示例中的 `progress` 和 `risks` 可以并行，`brief` 等两者成功后读取其结构化结果。可在网页查看回答和证据，重新打开历史任务查看依赖、尝试次数和状态。默认由 API 内的 Worker 调度，步骤存于数据库。

## 用单独进程运行

在启动 API 前，将 `AGENT_PLAN_WORKER` 设为 `external`。在其余终端设置相同的 `AGENT_DATABASE` 和 `AGENT_SCENARIOS`，然后分别运行 `python -m app.worker`。API、Worker、SQLite 文件和场景快照应在同一台机器上。独立 Worker 使用本机个人身份；自定义进程内授权表应搭配嵌入式 Worker，确保授权来源一致。可配置 `AGENT_PLAN_LEASE_SECONDS`（1–120 秒，默认 12）。Worker 退出后，剩余 Worker 等待租约到期即可接手。

## 通过 API 提交

向 `POST /api/v1/agent/chat` 提交下列 JSON，使用当前用户的 Bearer 令牌和本次提交专用的 `Idempotency-Key`。

```json
{
  "business_id": "project_brief",
  "message": "请为本周项目准备一份简报。",
  "execution_mode": "durable_plan",
  "mode": "offline",
  "response_mode": "blocking",
  "budget": {"max_steps": 5, "max_parallel_steps": 2, "max_tool_calls": 5, "deadline_ms": 120000}
}
```

显式选择 durable_plan 时，JSON 提交通道返回 **202** 与 `Location`，直接请求该地址读取最终任务。选择 `response_mode=streaming` 时返回 **200 SSE**。原有 reactive 请求的阻塞和流式响应继续返回 200。P1 规划器从受信任场景的计划文件产生确定性计划；模型模式沿用 reactive。客户端不能提交可执行计划、目标地址或权限。

| 方法与路径 | 返回 |
| --- | --- |
| GET /api/v1/agent/tasks/{id} | 任务、结果、证据、工具执行记录和错误 |
| GET /api/v1/agent/tasks/{id}/plan | 已授权的计划摘要、依赖、快照标识、各步状态 |
| GET /api/v1/agent/tasks/{id}/steps | 步骤列表、尝试编号、失败代码 |
| GET /api/v1/agent/tasks/{id}/events | 支持 Last-Event-ID 的 SSE 重放 |
| POST /api/v1/agent/tasks/{id}/cancel | 停止后续调度，旧租约的迟到结果无法提交 |

## 接入自己的场景

把 [plan.json 示例](../../examples/durable/scenarios/project_brief/plan.json) 与项目自己的 `scenario.json`、`adapter.py` 放在同一目录。每步通过 `capability_id` 选择已注册的只读工具。将请求文本绑定为 `{"$message":true}`，或用 `{"$step_output":"progress","path":"/data/summary"}` 引用前驱已声明的输出。支持直接和传递依赖；路径只指向允许的输出字段。调度器验证依赖、类型、权限、对象范围和预算。结果契约 `summary_v1` 读取成功末端步骤的 `data.summary`，汇集各步证据。允许降级的可选步骤失败时，结果附带缺失输出和限制。权限错误与数据结构错误会令任务失败。

服务端上限为 12 步、24 条依赖边、6 层深度、3 步并行、12 次逻辑工具调用和 15 分钟任务期限。步骤领取、工具缓存重放与物理 HTTP 重试分别计数。默认读工具可进行一次有界的暂时故障重试。

API 按内容摘要保存场景清单、计划、适配器及资料文件。场景需要额外的本地 JSON、TXT、CSV 或 Markdown 数据文件时，通过清单的 `snapshot_files` 显式列出同目录文件名，适配器按自身文件目录读取。包外的 Python 安装依赖与外部 API 数据由项目管理。发布新场景时重启 API 和 Worker 以装载新配置；已有计划使用自己的快照，并在实际执行时重新检查已加载的权限。备份 SQLite 时也要保存数据库旁的 `plan-snapshots/` 目录。

## 验收与语义

已持久化成功的步骤不会重新调用适配器。进程在只读调用途中退出时，接手方可能重新读取该步骤。租约令牌阻止旧执行者提交结果；已经记录的调用结果优先复用。取消不能撤销已经发出的外部请求，因此此执行模式只接受只读工具。正式写操作、审批和生产级分布式部署不在 P1 的可执行范围内。

```sh
python -m pytest tests/test_worker_recovery.py -q
python -m pytest -q
```

进程故障用例会中止正在执行汇总步骤的真实 Worker 和 API，再更换场景版本并启动 API 与两个新 Worker，核对旧任务和新任务分别运行的版本。[升级基线](upgrade-baseline.md) · [验收记录](../verification.md)
