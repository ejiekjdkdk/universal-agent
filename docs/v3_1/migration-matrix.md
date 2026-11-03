# V1/V2/V3 到 V3.1 首轮映射

| 来源能力 | 当前归属 | 本轮动作 | 不应推断 |
| --- | --- | --- | --- |
| V1 API/会话/三入口/权限 | main、runtime、identities | 保留与契约回归 | 不按示例改路径/参数 |
| V2 DAG/状态/审批/恢复 | plans、plan_store、approvals、worker | 保留 | 非PostgreSQL/分布式验收 |
| V2 Release/灰度/回滚 | snapshots、releases | 保留 | 不重新建立Registry |
| V3 Skill/引用/本地治理 | skills、skill_routes、skillctl | 增加离线DevKit，复用解析 | 本机HMAC不等于PKI |
| V3 输入拼接/预算 | providers | 增量ContextCompiler | 请求内引用不等于长期记忆 |
| V3 MCP | mcp_gateway | 本轮暂不改，记录锁定SDK | 新缓存/漂移/stdio仍待P2 |
| V3 两项目 | examples/v3/demo_a、demo_b | 增加独立demo_c | 不把行业分支加入核心 |
| 设计 Provider Router | 单提供者受控循环 | DESIGN_ONLY，P3 | 不能声称真实模型能力核验 |
| 设计加密大结果库 | 现有完整工具结果持久化 | DESIGN_ONLY，P2 | P1不新增结果读取API |
| 设计 Replay | 已有事件重放 | DESIGN_ONLY，P3 | SSE重放不是确定性推理回放 |
| V4/V5 架构愿景 | 无本轮实施 | 暂不改 | 不扩大版本范围 |

本轮无需权威数据库迁移、生产配置替换或外部写入。
