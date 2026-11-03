# V3 本机框架验收与升级

本次在 P0/P1 基线 `94701a4` 上补齐 P2 MCP、P3 技能持久任务与触发、P4 本地 SkillOps，并提供默认关闭的 P5 专业 Agent 工具示例。保持通用场景包和公开 API，不接入真实企业项目。数据与外部模型响应使用模拟内容；MCP 与触发入口使用实际本机 HTTP 连接。

## 已实现范围

| 模块 | 可复核行为 | 使用说明 |
| --- | --- | --- |
| Skill 与 Harness | 标准 SKILL.md 草稿导入、按需引用、冻结内容、输入和结果契约、实际调用预算；原 API 和无技能场景保留 | [双项目工作台](../../examples/v3/README.md) |
| MCP | 官方 SDK 2.3.0，明确批准 2026-07-28 协议；受信任端点和工具映射、租户凭据、Schema、超时、取消、撤销及固定配置摘要 | [MCP](mcp.md) |
| 持久任务 | 技能版本与计划一起固定；复用依赖、租约、重试、幂等、审批、取消和恢复；SQLite 实际双 Worker 竞争 | [计划协议](../v2/durable-plans.md) |
| 触发器 | HMAC 事件、时间窗和 nonce、事件去重、同事务创建任务、限流、定时游标与重启、实时撤权 | [触发器](triggers.md) |
| SkillOps | 校验、暂存、激活、灰度、回滚、吊销，固定评测与两版本比较、冻结作者和审核者、本机签名凭证、管理 API 与审计 | [SkillOps](skillops.md) |
| Trace | task / skill / tool / MCP 的关联、OTLP 属性、网页重放、专业委派父子执行记录 | [API](../api.md) |
| 专业 Agent | 两个确定性只读专业处理器作为工具；同任务身份、范围、取消、预算和执行证据；拒绝递归 | [可选示例](../../examples/specialists/README.md) |

## 设计 T01–T40 的本地验证路径

以下映射说明测试实际覆盖的边界，不将模拟业务结果等同于真实生产联调。

| 用例 | 证据文件与检查 |
| --- | --- |
| T01–T06 | `test_skill_packages.py`、`test_harness_skills.py`、`test_skill_governance.py`：草稿不可发现、逐步读取引用、依赖缺失、路径越界、禁止脚本、实时吊销。validated/staged 不改变原无技能业务路由，旧发布迁移期间及吊销后仍保留门禁。 |
| T07 | `test_v3_demos.py`、`test_external_template.py`：仓库外安装项目、两个业务独立运行、底座无需改动；另有实际浏览器验证。 |
| T08–T09 | `test_releases.py`、`test_release_flow.py`、`test_skill_governance.py`、`test_mcp_gateway.py`：不兼容发布、冻结版本、灰度稳定分桶、回滚、MCP 首次使用配置禁止漂移。 |
| T10–T16 | `test_mcp_gateway.py`：真实 SDK 服务的未注册端点、版本不兼容、工具缺失和重名、输入输出错误、超时和取消、工具描述及返回内容不扩大授权。 |
| T17–T22 | `test_api.py`、`test_security.py`、`test_providers.py`、`test_harness_skills.py`、`test_v3_durable_skills.py`：可信身份、越权、撤权、模型及物理重试预算、损坏输出、无执行证据不可成功、技能 Schema 错误统一分类。 |
| T23–T24 | `test_plans.py`、`test_plan_store.py`：循环依赖、非法输出引用和路径、计划执行前校验。 |
| T25–T29 | `test_durable_api.py`、`test_plan_store.py`、`test_worker_recovery.py`、`test_approval_recovery.py`：SSE 续读、唯一终态、双 Worker 租约、实际进程强制退出再恢复、取消一致性。 |
| T30–T35 | `test_catalog.py`、`test_mcp_gateway.py`、`test_security.py`、`test_approvals.py`、`test_v3_demos.py`、`test_api.py`：固定地址、SSRF 与重定向拒绝、凭据不出现在事件中、禁止真实写入、来源不足与知识权限、旧客户端兼容。 |
| T36 | `test_triggers.py`：重复事件只建一个任务，重复 ID 不同字节冲突、nonce 防重放、两连接竞争、失败事务回滚、定时重启与权限缩小。 |
| T37 | `test_observability.py`、`test_v3_durable_skills.py`、`test_mcp_gateway.py`、`test_specialists.py`：固定技能快照、工具执行与 MCP/委派事件、OTLP 和脱敏。 |
| T38–T39 | `test_approval_recovery.py`、`test_release_flow.py`、`test_mcp_gateway.py`、`test_skill_governance.py`：模拟写入结果未知不盲重试、旧任务实时撤销。 |
| T40 | `test_evaluations.py`、`test_snapshot_evaluation.py`、`test_skill_governance.py`：固定评测报告可重复、比较两版本、失败阻断发布。SkillOps 固定安全套件不衡量任意技能答案的语义质量。 |

## 本机执行记录

环境为 Windows、Python 3.11.2、隔离虚拟环境，依赖固定在 `requirements-win-py311.lock`。安装后 `python -m pip check` 检查依赖一致性。

```powershell
python -m pytest -q --tb=short
python -m compileall -q app scripts examples
node --check static/app.js
python scripts/local_triggers.py --verify
```

2026-11-03 最终全量回归：**370 项测试和 12 个子测试通过，107.80 秒**。Python/JavaScript 语法检查、依赖一致性检查及 Git 差异空白检查通过。测试包含真实临时 SQLite、SDK MCPServer 回环 HTTP、模拟模型、两个独立项目和进程恢复，不需要生产凭据。最终评审的待发布路由、迁移窗口门禁与 Schema 错误分类均已复现、修复并纳入回归。

对六个新增/扩展模块 `app/mcp_gateway.py`、`app/triggers.py`、`app/trigger_routes.py`、`app/skills.py`、`app/skill_routes.py`、`scripts/skillctl.py` 执行 mypy 1.18.2，使用 `--ignore-missing-imports --check-untyped-defs --follow-imports=silent`，全部通过。这不是全库类型零错误声明；P1 历史类型债务见[历史记录](acceptance.md)。

实际无头 Chrome 通过双项目启动脚本运行、切换业务、选择技能、查看证据与历史。Demo A 任务 `task_6d683c6e5d654b27b7ec8ceaeea8ebdb` 返回 1 条证据，Demo B 任务 `task_183c162c170d4804a21a1034a1f573c4` 返回 2 条证据，均显示所选技能。临时库随后清理，按指南重跑会产生新 ID。`scripts/local_triggers.py --verify` 另启动实际 API，验证签名事件、重投、拒绝篡改，以及事件和定时任务执行完成。

## 升级与回退

1. 停止 API 和 Worker，备份任务数据库、技能注册表目录、受信任配置。不要覆盖旧备份。
2. 新建 Python 3.11 虚拟环境并安装锁定依赖。不要沿用早期 AnyIO 4.0 环境；官方 MCP SDK 依赖更新后的异步接口。
3. 首次打开 P1 注册表会将缺少治理凭证的历史发布隔离为 `legacy_pending`。按 [SkillOps 迁移命令](skillops.md#p1-升级)提供可信场景目录和不同的作者/审核者，先迁移历史保留版本，最后迁移期望的稳定版本。旧任务的技能 digest 和 snapshot 保留。测试使用真实 P1 五表结构验证此兼容路径。
4. 注册表创建的 `.operator-key` 是本机 HMAC 信任材料，已加入 Git 忽略；应与注册表一起备份。更换或丢失密钥后不能继续信任旧凭证。
5. 运行双项目指南及需要启用的扩展验收。MCP、触发器与专业 Agent 默认不启用；按独立文档配置。
6. 回退时使用升级前备份和独立的 P1 源码目录 `94701a4`，不要用旧程序打开升级后的数据库。保留新目录供审计与再次升级。

## 明确边界

- SQLite 是设计允许的本机后端。生产 PostgreSQL 适配器、分布式消息队列和企业部署尚未实现或验证；当前恢复结论限本机数据库和进程。
- 没有进行真实模型、企业数据源或第三方生产 MCP 服务联调。当前 MCP 仅支持批准协议和字面 IP 的 Streamable HTTP，不支持 DNS/私网企业端点、stdio 或自动启动外部进程。
- SkillOps 使用本机 HMAC 和本地操作者身份声明；不是企业 PKI、SSO 或独立人员审批。管理接口有独立管理员令牌，真实业务权限仍由项目授权规则控制。
- 专业 Agent 是可选的确定性工具委派框架，不包含自主多模型协作。持久任务使用显式步骤展开专业能力，不将嵌套委派伪装成独立可恢复步骤。
- 回答的来源标签与真实调用绑定，但来源是否充分支持语义结论仍需业务评测。安全扫描与敏感信息规则只覆盖明确的检查范围。
