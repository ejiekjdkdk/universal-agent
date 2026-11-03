# P0 实现与环境审计

日期 2026-11-03；代码基线 ac448df。两份用户原始设计已保存到 docs/design/V3.0.md、V3.1.md；它们是目标规格，以下代码事实优先。

| 职责 | 实际实现与证据 | 结论 |
| --- | --- | --- |
| REST/流式响应 | app/main.py:121 create_app；ChatInput:58；tests/test_api.py、test_security.py | 已实现；本轮保留 |
| 推理循环 | app/providers.py:295 _model_loop、391 run_provider；test_providers.py、test_model_flow.py | 已实现；无单独 Harness 类，不必增加包装 |
| 上下文 | providers.py:406 起组装系统/选中技能/有限历史；runtime.py:469 授权历史 | 已有限制与鉴权，缺少动态预算编译 |
| 唯一网关/策略 | runtime.py:32 Gateway、52 context；Runtime.authorize_task | 已实现；新增引用读也必须调用原鉴权 |
| 持久执行 | Store/PlanStore/ApprovalStore、worker.py PlanWorker | 已实现 SQLite 租约、恢复与 Mock 审批；不是 PostgreSQL |
| 技能 | skills.py:178 _package、232 SkillRegistry；skill_routes.py、scripts/skillctl.py | 已实现冻结/治理，缺离线 init/pack/工件 inspect |
| MCP | mcp_gateway.py:97 MCPGateway、catalog.py | mcp==2.3.0，批准2026-07-28，字面IP Streamable HTTP；不增加DNS/stdio |
| 触发 | triggers.py:86 TriggerGateway | 已实现来源校验与持久去重，不重建 |
| 可观测 | observability.py、Store.events/audit | 已实现；新编译不向旧 SSE 加枚举 |
| 业务扩展 | examples/v3/demo_a、demo_b；test_v3_demos.py | 已独立部署，第三个示例只加项目包 |

Python 3.11 隔离环境沿用旧交付，依赖来自 requirements-win-py311.lock，pip check 无冲突；没有安装新库。新工作区基线回归：387 passed、12 subtests passed，181.04秒。快照回归新增后单独通过1项。实际环境与结果在 test-inventory.md 更新。

官方 MCP 网页查询在本轮连接失败，不声称已核验最新在线协议；当前 SDK 能力按锁定安装源码与已有实际回环测试判定。旧实现没有统一 Context Manager/Provider Router 类，无需按设计图增加空壳。

可行性结论：P0/P1 可以原位增强，不需要破坏性迁移。P1 请求内临时内容引用不等于 P2 加密大结果数据库；真实模型画像、故障切换和 Replay 留后续阶段。当前无需要用户重新选择的平台或业务范围。
