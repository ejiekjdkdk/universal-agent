# Universal Agent

Universal Agent 是一套可跨项目复用的通用 Agent 底座。它负责模型接入、工具发现与调用、权限校验、会话、任务、证据和执行轨迹（Trace）。接入新项目时，将该项目的业务接口或处理逻辑封装成场景包，由项目自己的前端或后端通过统一 API 发起任务。

例如，给工单系统增加助手时，可以注册一个“查询工单”工具，在适配器中调用现有工单 API，并按项目的身份和数据权限限制可查范围。模型选择工具、记录调用结果和回放执行过程的逻辑可直接复用。[场景接入指南](docs/scenarios.md)

当前 main 已包含 V3.1 P0～P4，在约定的本机范围内完成开发和验收。随附的网页供开发者切换场景、选择技能、提问、核对证据和调试 Trace。「资料问答」与「文本分析」是两个可独立移除的示例场景。

**后续开发以 V3 为目标和迭代基准。** 先完善已有功能、修复缺陷并降低阅读和维护成本。V4、V5 作为长期架构愿景，具体能力经单独评估和确认后再纳入实施，不提前增加抽象层或依赖。[迭代约定与代码阅读顺序](docs/v3/development-baseline.md)

## 版本迭代历史

以下按开发顺序记录各阶段相对上一阶段的增量。V2、V3、V3.1 各自使用 P0/P1 等阶段编号，不能跨版本混用；设计文档中的愿景不等于已交付能力。后续更新应在对应版本下追加变化与验收记录，保留已有版本历史。

### V1 · 通用底座与独立项目接入

- **首版运行底座**：建立场景包加载、工具发现与调用、输入输出 Schema、身份及对象权限、调用幂等、取消、SQLite 会话与任务持久化，以及 REST/SSE、Trace 和历史查看；提供离线模式与兼容模型接入。
- **双场景验证**：先实现资料问答，再独立加入可移除的文本分析场景；网页可切换业务、查看来源证据和恢复历史，用新增场景验证业务无需写入核心。
- **独立项目模板**：增加仓库外项目的后端与适配器示例，通过公开 HTTP API 调用底座，验证项目权限、上游失败和同键重放。

[V1 设计](docs/design/V1.0.md) · [首版与独立项目验收](docs/verification.md) · [项目接入模板](templates/external-project/README.md)

### V2 · 可恢复任务、审批与发布治理

| 迭代 | 相比上一阶段新增或改进 |
| --- | --- |
| V2-P1：持久只读计划 | 增加 `durable_plan` 多步骤依赖图、并行读取与汇总、独立 Worker、租约围栏、重试和进程故障恢复；冻结场景版本，网页显示步骤与尝试次数，保留 V1 reactive 请求、旧库历史及幂等行为。 |
| V2-P2：人工审批与模拟写入 | 增加申请人与审批人分离、提案摘要与对象版本绑定、审批页、过期/撤权复查，以及本地模拟账本和唯一回执；未知结果通过只读查证恢复，不盲目重发。 |
| V2-P3：发布、评测与观测 | 增加不可变发布快照、候选校验、灰度分桶、激活、逐级回滚和紧急禁用；增加固定离线评测、安全探针、指标告警与脱敏 OTLP 导出，并用独立模拟变更项目验证流程。 |
| V2 后续整理与修复 | 简化运行与调度代码、统一中文注释；修复评测快照与执行内容不一致、旧场景兼容、Trace 版本归属等问题，补强权限、快照隔离及并发边界。 |

运行 `python scripts/local_governance.py` 可体验本地申请和审批，添加 `--verify` 可验证批准重放、模拟回执、发布与回滚。V2 的写入验收限本地模拟账本。

[V2 设计](docs/design/V2.0.md) · [持久计划与故障恢复](docs/v2/durable-plans.md) · [审批、发布与回滚](docs/v2/governance.md) · [验收记录](docs/verification.md) · [整理与修复记录](docs/audits/readability-v2.md)

### V3 · Skill 与通用 Agent Harness

| 迭代 | 相比上一阶段新增或改进 |
| --- | --- |
| V3-P0/P1：环境基线与 Skill 接入 | 核对运行环境与原接口，支持标准 `SKILL.md` 草稿导入、按需读取引用、发布内容冻结、技能输入/结果契约与调用预算；提供两个仓库外独立项目，网页增加技能选择，保留无技能场景。 |
| V3-P2：MCP 工具 | 通过官方 SDK 接入批准协议的 Streamable HTTP，增加受信端点与工具映射、租户凭据、Schema 校验、超时、取消和配置冻结；没有开放任意地址或自动启动外部进程。 |
| V3-P3：技能持久任务与触发器 | 将技能版本绑定到原持久计划；增加 HMAC 签名事件、时间窗与 nonce、防重放、事件去重、定时游标及重启恢复，沿用权限、审批、租约和取消机制。 |
| V3-P4：本地 SkillOps | 增加技能校验、暂存、激活、灰度、回滚、吊销、固定评测与版本比较，以及作者/审核者记录、本机签名凭证和旧技能迁移。 |
| V3-P5：可选专业工具示例 | 提供默认关闭的两个确定性只读专业处理器，传递当前任务身份、权限、预算、取消及父子执行证据并拒绝递归；不代表自主多模型协作已完成。 |
| V3 后续整理与审计修复 | 改善可读性并保留接口与恢复语义；修复 SSE 已缓存事件在撤权后继续发送、触发来源撤销后模拟写入仍可能落账的问题，并补充 Windows 子进程文件释放兼容处理。 |

[V3 设计](docs/design/V3.0.md) · [P0/P1 验收](docs/v3/acceptance.md) · [P2～P5 本机验收](docs/v3/completion-acceptance.md) · [可读性整理](docs/audits/readability-v3.md) · [安全审计修复](docs/audits/v3-safety-audit.md)

### V3.1 · 上下文、开发工具、模型路由与本机发布检查

截至 2026-11-04，P3、P4 均已合并到 `main`；功能分支与本地增量历史保留。V3.1 主体开发已完成，以下状态按本机验收范围说明。[V3.1 设计文档](docs/design/V3.1.md)

**V3.1 P0 兼容基线**固定 V3 的 OpenAPI 路径、公开方法、数据库结构与终态映射，建立迁移和测试清单，作为后续增量保持旧接口、权限和恢复行为的检查依据。[兼容基线](docs/v3_1/compatibility-baseline.md) · [测试清单](docs/v3_1/test-inventory.md)

**V3.1 P1 上下文与 Skill DevKit**提供默认关闭的上下文编译器，按预算组织模型输入并保留来源、版本与时点；增加离线技能初始化、校验、测试、打包、工件检查和环境诊断，以及第三个虚构集合计数项目，复用原网关、执行器与发布流程。入口为 `python scripts/agent.py`，工件检查不会自动安装或发布技能。[启用与使用](docs/v3_1/usage.md) · [实施范围](docs/v3_1/design.md) · [P0/P1 验收](docs/v3_1/acceptance.md) · [第三项目](examples/v3/demo_c/README.md)

**V3.1 P2 增量**提供默认关闭的加密完整结果及授权分页、有时效的只读缓存与并发合并，以及可选MCP目录批准、漂移阻断和健康策略。默认结果保留期为任务结束后24小时，可配置；旧API仍返回完整data。[P2使用与回退](docs/v3_1/p2-usage.md) · [MCP支持与拒绝矩阵](docs/v3_1/p2-mcp-matrix.md) · [P2本机验收](docs/v3_1/p2-acceptance.md)。仅完成本机验证的范围可用，stdio和真实企业接入不在已支持范围内。

**V3.1 P2 后续整理与审计修复**改善可读性，并补强撤权后的上下文复用、MCP 诊断内容保护、外部 Schema 计算限额及检索/缓存依赖检查；修复随全量回归合入 main。[两轮审计集成记录](docs/audits/v31-two-audits-integration.md)

**V3.1 P3 模型路由与 Replay**提供基于可信能力画像与业务策略的模型选择、严格结构化输出校验，以及限流、超时和断流后的有界切换。切换时复用已成功且仍获授权、未失效的工具结果，未知副作用继续走原恢复流程。Replay 提供显式本地录制、脱敏导出、回放与差分，只使用虚构或已脱敏夹具，不自动记录日常网页/API 任务，不回退真实网络或执行写工具。未配置路由策略时保留原模型路径。[P3 使用与回退](docs/v3_1/p3-usage.md) · [P3 本机验收](docs/v3_1/p3-acceptance.md) · [虚构示例](examples/v3_1/p3/README.md)

**V3.1 P4 环境与本机发布检查**已使用独立 Python 3.11.18 / SQLite 3.53.1 完成验收，保留旧环境和既有 API；覆盖真实 HTTP SSE 断线重连、双 Worker 模拟写入恢复及代码/配置回退。检查区分环境通过与完整本机就绪，Replay 指纹同时绑定实际 SQLite 原生身份。[环境、门禁与回退](docs/v3_1/release-readiness.md) · [P4 验收与边界](docs/v3_1/p4-acceptance.md) · [接口兼容矩阵](docs/v3_1/p4-interfaces.md)

P3 后续审计修复了候选凭据传播、模型结束原因校验、取消用量审计及 Replay 环境确定性问题；P4 修复了发布检查遗漏收集跳过、取消选择和场景目录测试的问题。[P3 审计记录](docs/audits/v31-p3-review.md) · [P4 审计记录](docs/audits/v31-p4-review.md)

**P4 合入 main 时的回归**：代码基线 `05fd862` 通过 652 项测试及 12 项子测试；六项辅助检查通过。该数字是 P4 阶段记录。

**2026-11-05 · V3.1 本机收尾**（接续 P4）：

| 本次变化 | 相比 P4，使用者可以做什么 |
| --- | --- |
| V31-020：签名技能包安装 | 用 `skill install` 把通过 HMAC、文件摘要和依赖检查的 ZIP 导入现有注册表，结果是无权限草稿；篡改、未签名或密钥错误均拒绝。激活仍需经过原 SkillOps 审核。[安装示例](docs/v3_1/usage.md#签名工件安装) |
| Windows 默认启动 | 每份检出只需登记一次独立解释器；之后双击 `start.cmd` 会核对 Python、实际加载的 SQLite 和锁定依赖，再启动工作台。校验失败会停止，原环境得以保留。[登记与复验](docs/v3_1/release-readiness.md#登记默认启动环境) |
| 恢复副本的结果读取 | 在独立恢复的副本上验证：仍获授权的结果可解密并分页读取，已撤权的结果继续拒绝，读取历史不会再次执行业务工具。[恢复验收](docs/v3_1/p4-acceptance.md#收尾补验) |

商业模型、真实企业写入、PostgreSQL、跨主机分布式及生产发布仍未验收；本机通过不等于生产就绪。

## 启动

**V3 本机框架**支持标准 `SKILL.md` 草稿导入、绑定技能的可恢复计划、MCP 工具、签名事件与定时触发，以及技能校验、暂存、灰度发布、回滚和吊销。可选专业 Agent 工具默认关闭。两个独立项目分别演示虚构记录查询和文档片段归纳，无需修改底座核心代码。参见[双项目运行指南](examples/v3/README.md)和[完整验收及部署边界](docs/v3/completion-acceptance.md)。

扩展入口：[MCP 配置](docs/v3/mcp.md) · [事件与定时触发](docs/v3/triggers.md) · [SkillOps 与旧技能迁移](docs/v3/skillops.md) · [专业 Agent 示例](examples/specialists/README.md)。升级 P1 前先备份数据和技能目录；旧发布需显式迁移，签名密钥 `.operator-key` 必须随注册表保管。当前采用 SQLite；生产 PostgreSQL 适配器未实现，真实模型和企业系统未联调。

Windows 首次使用请按[登记默认启动环境](docs/v3_1/release-readiness.md#登记默认启动环境)选择已验收的独立 Python。以后双击或执行 `.\start.cmd`，打开 <http://127.0.0.1:8765>；只想核验环境可执行 `.\start.cmd --check`。启动器不会调用系统 Python 建环境或自动安装依赖。个人模式只监听本机，按 Ctrl+C 停止。

其他系统，或已有 Python 环境时，可使用以下基础启动命令。

```sh
python -m venv .venv
# 在 macOS 或 Linux 中激活虚拟环境
. .venv/bin/activate
# Windows PowerShell 则运行 .\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python run.py
```

1. 选择「资料问答」，点击「使用示例」再运行。展开证据可看到样文标题、版本和原文片段。
2. 切换到「文本分析」，粘贴一段文本或使用示例。结果包含字符数、非空行数和高频词项。中文示例用空格分隔词项，连续的汉字计为同一词项。
3. 展开右侧执行轨迹查看工具、权限、输入摘要和耗时。刷新网页后，在「最近运行」中重新打开任务。继续提交会沿用当前会话，点击「新会话」开始独立对话。

## 离线与模型模式

**离线演示**无需密钥。每个场景在清单中指定一个工具，工作台把输入文本交给它。资料问答使用本地词项匹配，回答来自命中的样文。两个示例均可独立移除。

**模型模式**使用支持工具调用的 Chat Completions 兼容端点。将 `.env.example` 复制为 `.env`，设置服务的 API 基址（含实际路径前缀，如 `/v1`）和模型名，重启后在网页切换模式。

```ini
AGENT_MODEL_BASE_URL=http://127.0.0.1:8000/v1
AGENT_MODEL_NAME=填写本机服务中的模型名
AGENT_MODEL_API_KEY=
```

远程服务需配置 HTTPS 和服务端密钥。本地回环端点可省略密钥。模型模式会向该端点发送当前问题、当前会话的有限历史和所调用工具的结果。模型可发现能力、执行工具、检索资料，工作台将本轮来源编号为 `[E1]`、`[E2]` 等。模型返回错误或超时会记录在任务与 Trace 中。实际兼容性取决于端点能否处理工具调用和工具结果消息。

## 为自己的项目复用

已有独立项目时，可以从[独立项目接入模板](templates/external-project/README.md)开始。将模板复制到仓库外，项目后端用公开 API 提问，底座再调用项目自己的只读接口。随附的验收脚本会启动两个本地进程，核对回答、证据、Trace 和幂等重放，并在结束时关闭它们。

复制一个场景目录，修改清单、输入输出 Schema 和适配器，放入 `scenarios/` 后重启。也可让 `AGENT_SCENARIOS` 指向你自己的独立目录。配置可以为空；工作台仍可启动并展示空状态。场景包支持本地 Python 工具、固定目标的 HTTPS 工具、可选本地资料和权限码。[场景接入指南](docs/scenarios.md)

前端或其他程序使用 REST 与 SSE。开发配置可设 `AGENT_ACCESS_TOKEN` 为自己的高熵令牌；服务重启后，网页会要求输入令牌。下例从调用端环境读取同一个令牌。

```powershell
$headers = @{ Authorization = "Bearer $env:AGENT_ACCESS_TOKEN" }
$body = @{ business_id = "notes"; message = "如何记录会议纪要？"; mode = "offline" } | ConvertTo-Json
Invoke-RestMethod http://127.0.0.1:8765/api/v1/agent/chat -Method Post -Headers $headers -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($body))
```

```sh
curl -N http://127.0.0.1:8765/api/v1/agent/chat \
  -H "Authorization: Bearer $AGENT_ACCESS_TOKEN" -H "Content-Type: application/json" \
  -d '{"business_id":"notes","message":"如何记录会议纪要？","response_mode":"streaming"}'
```

[API、Trace 与存储](docs/api.md) · [验收记录](docs/verification.md)

## 测试

```sh
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

测试使用临时 SQLite、可移除的示例和模拟的模型 HTTP 响应，可在没有模型账号时复现。
