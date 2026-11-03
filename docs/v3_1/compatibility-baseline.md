# V3.1 兼容基线

以 ac448df 为准。机器可读快照 tests/fixtures/v31-baseline.json 由 scripts/compatibility_snapshot.py 在产品修改前生成，包含38条 OpenAPI 路径、公开方法参数、SQL 建表/索引定义及终态映射。tests/test_v31_compatibility.py 对照验证；REST/SSE 运行行为由现有 API、安全与持久任务测试负责，文本快照不能替代执行。

- ChatInput 保留 mode=offline/mock/openai、response_mode=blocking/streaming、execution_mode=reactive/durable_plan，默认离线即时调用。
- 工具记录名为 execution_id、调用成功状态 completed；不改为设计示例的 tool_execution_id/succeeded。
- TERMINAL_EVENTS 中 partial 使用 task_completed；不新增 task_partial。SSE id/event/data 和 Last-Event-ID 原样保留。
- 对外鉴权由 tenant_id/org_id/user_id、业务和对象范围共同决定。用户输入不产生可信身份。
- Plan Schema 仍是2.0，Skill manifest 可省略 schema_version，旧标准 SKILL.md 仍可草稿导入。不为版本数字批量改旧包。
- 原技能发布CLI、管理员令牌及本地 HMAC 信任保持不变；新 DevKit 不发布。
- 无数据库迁移。新增编译器默认关闭，开启后新增 CONTEXT_BUDGET_EXCEEDED/422、CONTEXT_SOURCE_EXPIRED/410 等只属于新路径；旧预算与错误码不变。
- 旧接口返回完整 data/result，不被模型输入摘要替换；新增审计写现有 audit 表，不增加 SSE 枚举。

回退：关闭 AGENT_CONTEXT_COMPILER，继续使用旧 skillctl；需要源码回退时使用独立旧检出和原配置，无需回填数据。
