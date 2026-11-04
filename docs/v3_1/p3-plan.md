# V3.1 P3 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans。用户已明确两项范围并授权没有未决问题后开发；当前会话逐项实施，最后独立审查，不重复请求同一范围批准。

**Goal:** 增量交付可信模型路由、安全故障切换和本地录制回放差分。
**Architecture:** 唯一原 Runtime/Gateway/Store/Worker；新增 model_router 与 replay 是策略及开发工具，不持有第二份业务状态。
**Tech Stack:** Windows Python 3.11、原 httpx/jsonschema/SQLite 和标准库，不新增依赖。
**Spec:** docs/v3_1/p3-design.md；docs/design/V3.1.md §6/§8。

## Global Constraints

- 基线 main@82b1a73；默认路径、公开签名/OpenAPI/SSE/数据库不变。
- Replay 仅显式本地测试；Mock HTTP/虚构数据验收；真实商业模型 NOT_RUN。
- 中文注释；不执行 Skill 或 Replay 脚本；不开放写权限；预算和授权失败关闭。
- 代码分支 codex/v31-p3，保留 main 和历史分支，不自动合并 main。

## Review Focus

- 第一次工具已成功后换 provider 和 call_id：Task2 验证仅一条物理执行和一次证据。
- 小窗口候选及来源撤销：Task2 验证候选发送前重新编译/鉴权，不能回退绕过。
- 断流或失败却无 usage：Task1 验证无半截工具、预留不退、未知费用不伪装已知。
- 工件篡改/敏感值/路径及适配器偷换：Task3 验证在执行前拒绝且无网络回退。
- 比较输入不同或权限/证据退化：Task3 区分 INCOMPARABLE 与 FAIL，不报虚构质量提升。

## Task1 画像、预算和协议

Files: app/model_router.py；tests/test_model_router.py。
Interfaces: load_policy(settings)->dict|None；ProviderRouter(settings,gateway,tools)；async complete(messages,context_session,authorize)->(reply,usage)，rates 为当前画像价格，generation 记录切换代次。私有 _audit 只写 Store.audit。

- [x] 测试先失败：缺失/未知能力、过期画像、类别/区域不匹配、禁用、坏配置；相同正常配置可用。
- [x] 实现严格策略加载、冻结摘要与实时复查、候选过滤及任务内失败隔离。
- [x] 测试先失败：严格格式请求、429/503/timeout、断流/拆分工具参数、refusal/截断/重复键、多 choice、预算耗尽/未知 usage、取消。
- [x] 实现有界 JSON/SSE，完整解析才返回；候选分别定价，失败消耗保守预留；测试全部通过后提交。

## Task2 原模型循环集成

Files: app/providers.py、app/main.py；tests/test_provider_router_integration.py。
Interfaces: _model_loop 新增私有关键字 router=None；run_provider 公开签名不变；P3 路径强制当前结构化结果及 ContextSession。main 只扩展 model_ready 判断。

- [x] 用实际 Store/Runtime/Gateway 写失败复现：主模型先调用工具，再429，候选新ID重复同参数；执行条数仍为1。
- [x] 加请求边界切换和同参数结果复用；复用时检查来源及分页引用，不重跑无效结果。
- [x] 验证较小窗口候选、撤权、配置轮换、只读限制、预算、无新增SSE、旧模式/公开契约不变；专项通过后提交。

## Task3 Replay 工件和差分

Files: app/replay.py、scripts/replay.py；tests/test_replay.py。
Interfaces: record_case(case,out,key)->dict；inspect_bundle(path,key)->dict；run_replay(path,key)->dict；compare_bundles(baseline,candidate,key)->dict。命令 record --case --out --key-env / inspect --bundle --key-env / run --bundle --key-env / compare --baseline --candidate --key-env。

- [x] 先写失败测试：虚构用例走实际 Gateway/任务结算，重复回放结构相同，失败用例亦可复现。
- [x] 实现固定路径/有界 ZIP、SHA256/HMAC、显式脱敏、版本核验和无任何真实适配器的运行器。
- [x] 先写并实现篡改/重复路径/大小/秘密/写意图/无录制命中拒绝测试；断言无真实网络和脚本执行。
- [x] 先写并实现差分：输入摘要不等为 INCOMPARABLE；权限扩大、证据丢失、状态/Schema退化为FAIL；成本未知不计算精确收益。CLI端到端通过后提交。

## Task4 本机互通、验收与审查

Files: examples/v3_1/p3、docs/v3_1/p3-acceptance.md、p3-usage.md、p3-progress.md；tests/test_provider_http.py。

- [x] 用实际回环 HTTP 服务验证画像、严格 JSON、429/503切换、流断开与不重复执行；不把 MockTransport 称为真实HTTP。
- [x] 提供默认未启用的策略、虚构Replay示例与可复制命令；列 V31-036～043 状态及限制。
- [x] 完整 pytest、限定新增模块 mypy、compileall、node --check、pip check、git diff --check。
- [x] 独立审查全分支；严重问题先复现后修复，回归通过；交付完整源码、日志及本轮分支状态。
