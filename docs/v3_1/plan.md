# V3.1 P0/P1 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. 用户已授权评估后直接开发；本轮在当前会话实施，最终独立复核，不重复要求批准相同范围。

**Goal:** 在既有 V3 增量交付可选上下文编译、离线技能开发工具和第三个独立示例。

**Architecture:** 复用 Runtime/Gateway/Store/Worker/SkillRegistry。新增模块只负责模型输入及离线候选工件，不新增权威任务存储或发布系统。

**Tech Stack:** Windows Python 3.11，现有 requirements-win-py311.lock，不加依赖。

**Spec:** docs/v3_1/design.md；原文 docs/design/V3.0.md、V3.1.md。

## Global Constraints

- 保留 main@ac448df 的接口、SSE、状态、错误、权限与数据库结构；所有新增行为必须显式启用或通过新 CLI 调用。
- 中文说明性注释和 Docstring；有限函数与直观语法；不执行 Skill 脚本，不访问企业系统。
- P2/P3/P4 新能力 NOT_RUN，不声称生产完成；未核验的模型容量不能作为真实模型能力。
- 新功能在 codex/v31-p0-p1，未经后续指令不合并 main。

## Review Focus

- 历史/缓存/引用在授权撤销后不能继续入模，编译前和引用读取都复核 Gateway.context。
- 不同版本/时点的同名证据不能错误合并，摘要保留完整证据。
- 工具调用/响应不能被预算裁减为非法消息序列，必需内容放不下必须失败。
- 归档大小写冲突、重复条目、声明大小不实、摘要篡改、链接和凭据必须在候选工件处理时拒绝。
- 离线合同样本缺失/跳过不能计为 PASS，工件摘要不能冒充可信签名或正式发布。

## Task 1：P0 可复核基线

Files: docs/v3_1/{audit,compatibility-baseline,migration-matrix,test-inventory}.md；tests/fixtures/v31-baseline.json。

- [x] 记录现有模块、声明位置、DB/公开入口和三逻辑 Gateway 的映射。
- [x] 从未改动的基线生成 OpenAPI、DDL、公开签名与 SSE/状态常量快照。
- [x] 完整基线回归并记录结果；检查 Python、锁定 SDK 和 pip check。
- [x] 提交 P0 文档、原始设计和基线快照。

## Task 2：Context Compiler 与运行路径

Files: app/context_compiler.py、app/providers.py；tests/test_context_compiler.py、tests/test_context_integration.py；.env.example。

Interfaces: ContextSession(gateway, settings, tools) 接收既有 Gateway，add(message, source_kind, pinned, summarize=False, capability=None, arguments=None) 返回随机引用；read(ref) 在当前授权下返回原内容；compile() 返回 CompiledContext(messages_json, pinned_item_ids, omitted_item_ids, summary_refs, token_estimate, limitations)。ContextItem 为冻结的来源/作用域/消息/摘要/时点元数据。

- [x] 先编写纯编译和真实 Gateway 回归：跨作用域、TTL、伪 system、撤权、预算、不可变结果、保留不同证据时点、工具结果缩减与完整 API 结果、无新增 SSE 枚举。
- [x] 执行专项，观察新行为缺失导致失败。
- [x] 实现保守估算、可信角色校验、预算选择、只读任务引用和脱敏审计；不更改 Store Schema。
- [x] 将启用后的每回合接入现有 provider loop，保留未启用旧路径和原工具次数限制；read_context_reference 计入模型工具次数。
- [x] 专项通过后提交；最终全量覆盖旧路径。

## Task 3：Skill DevKit

Files: app/skill_devkit.py、scripts/agent.py；tests/test_skill_devkit.py。

Interfaces: init_skill(directory,business)、validate_skill(directory,capabilities)、test_skill(directory,capabilities)、pack_skill(directory,output,capabilities,signing_key=None)、inspect_artifact(path,signing_key=None)、doctor() 返回 JSON 可序列化结果。CLI 输出 JSON；校验/执行失败非零；缺少 fixtures 明确 NOT_RUN。

- [x] 先写 init 不覆盖、旧包兼容、依赖拒绝、正负 Schema 样本、禁止脚本/链接/凭据、同内容稳定归档、篡改/炸弹/重复路径与签名验证测试。
- [x] 运行观察功能缺失失败，再实现受限打包、工件验证及六个 CLI 子命令。
- [x] 通过真实 CLI 子进程测试与所有 SkillOps 老回归后提交。

## Task 4：第三示例与最终验收

Files: examples/v3/demo_c/**；tests/test_v31_demo.py；docs/v3_1/{usage,acceptance}.md；README.md。

- [x] 先写独立目录部署、工具真实执行、授权对象统计、证据、越权和重放测试；观察文件缺失失败。
- [x] 完成纯虚构项目包，通过 DevKit 测试/打包/检查，使用原 SkillRegistry API建立测试发布，开启编译器经 Mock 与真实 Gateway 执行；正式本机流程继续由skillctl治理。
- [x] 保存同任务编译前后结构/证据/调用数及保守估算对比，不声称真实模型 Token 降幅。
- [x] 逐项记录 V31-001～048 的 PASS/FAIL/NOT_RUN；完整 pytest、受改模块类型检查、编译、JS 和依赖检查。
- [x] 请求独立全分支复核，确认问题复现修复，记录复核未完成的限制；上传功能分支，交付完整源码和验收证据，不合并 main。
