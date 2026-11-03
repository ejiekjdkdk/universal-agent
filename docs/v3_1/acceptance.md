# V3.1 首轮 P0/P1 验收

日期：2026-11-03。基线：main@ac448dfe820624d4fbfbf1fae66ba05e3b3b426f。交付分支：codex/v31-p0-p1。完整源码包含两份原始设计、实现、示例和测试；本轮不合并 main。

## 可行性与完成范围

可在现有 V3 上增量实现，未发现需要用户重新决策的架构阻断。复用 Runtime/Gateway/Store/PlanWorker/SkillRegistry，保留 SQLite 和既有接口；没有新增第三方依赖。按 V3.1 文档的首轮范围完成可选上下文编译、离线 Skill DevKit、第三个独立项目及兼容回归。V3 仍是开发基线，V4/V5 是长期架构愿景。

编译器默认关闭，只投影模型输入，不改原始工具结果或终态清理策略。Schema、API字段、SSE枚举、数据库定义及既有公开入口受快照和回归检查。DevKit 生成草稿、校验、声明式测试、稳定打包和检查工件；不安装、发布或授予权限。第三项目使用原 SkillRegistry API 建立测试发布，再经真实 Gateway 执行；不是新安装流程，也不是独立人员审核演示。

V31-020 的摘要/签名检查子项已经验证，但规格中的“拒绝安装”没有实现路径，完整用例标为 NOT_RUN。不能据此声称 P1 全矩阵或整个 V3.1 已完成。

## 执行证据

- 未改动基线：387 passed、12 subtests passed，181.04秒。
- 新功能首轮全量：425 passed、12 subtests passed，238.92秒。
- 审计修复后最终全量：430 passed、12 subtests passed，196.68秒；命令 `python -m pytest -q --tb=short`。
- 撤销与提示注入专项：20 passed，26.31秒；DevKit及第三项目专项：24 passed，38.68秒。
- 新增三个模块类型检查：`python -m mypy --follow-imports=skip --ignore-missing-imports --check-untyped-defs app/context_compiler.py app/skill_devkit.py scripts/agent.py` 通过。普通无参数排除的 mypy 会遍历旧模块并报告11项存量/缺少stub问题；没有宣称全库类型检查通过，也未增加stub依赖。
- `python -m compileall -q app scripts examples`、`node --check static/app.js`、`python -m pip check` 通过。
- 修改文件空白检查通过。原始附件 V3.0.md/V3.1.md 保留原字节及Markdown双空格换行，不把它们的源格式当代码修复。

上述为 Windows Python3.11 本机测试：Mock模型输出、实际Gateway/SQLite/进程/HTTP协议分别标明。没有真实模型、企业服务、生产凭据或真实写入。官方MCP网页本轮访问失败，协议能力依据锁定SDK及既有本机回环测试，不是最新在线规范认证。

## 逐项映射

| 用例 | 状态 | 证据与边界 |
| --- | --- | --- |
| V31-001 | PASS | test_v31_compatibility 固定38条OpenAPI路径及结构；test_api |
| V31-002 | PASS | 原Task/Step、reactive/durable_plan回归；test_plan_store、test_worker_recovery |
| V31-003 | PASS | 入口签名快照；test_security、test_context_integration，原授权路径复用 |
| V31-004 | PASS | test_skill_packages、test_skill_governance；DevKit旧版本及无版本manifest |
| V31-005 | PASS | test-inventory.md 分开记录Mock、本机集成和Real NOT_RUN |
| V31-006 | PASS | Runtime/Gateway/Store/PlanWorker保持唯一执行链；无新任务库 |
| V31-007 | PASS | test_context_compiler：先引用大明细、再省略可选历史，策略必留 |
| V31-008 | PASS | 当前目标、任务状态及技能指令固定；真实运行接入和固定样本验证 |
| V31-009 | PASS | 同来源同版本不同时点分配不同引用；摘要保留完整evidence |
| V31-010 | PASS | 当前任务权限、能力紧急吊销、ContextItem有效期；非外部源实时TTL认证 |
| V31-011 | PASS | 主体/任务/业务scope与snapshot检查，跨任务引用拒绝 |
| V31-012 | PASS | 伪system拒绝；工具描述注入在开关两种状态均不能扩大技能权限 |
| V31-013 | PASS | 编译审计与limitations；大结果及长历史省略报告 |
| V31-014 | PASS | 必需内容或全文引用无法容纳时CONTEXT_BUDGET_EXCEEDED，工具不重复执行 |
| V31-015 | PASS | 实际CLI与目录不覆盖测试 |
| V31-016 | PASS | 非法Frontmatter/YAML/Schema报错；合同测试进程超时失败 |
| V31-017 | PASS | 能力依赖必须在操作者批准清单中 |
| V31-018 | PASS | 原路径规则、链接/硬链接拒绝；归档穿越、大小写/重复项与大小限额 |
| V31-019 | PASS | 固定字节、排序、ZIP元数据，重复打包摘要与归档一致 |
| V31-020 | NOT_RUN | 文件/摘要篡改、错误HMAC、非ASCII签名均拒绝检查；无安装器，未测安装 |
| V31-021 | PASS | test_v31_demo：外部目录、8件统计、2条证据、对象拒绝、幂等和SSE重放 |
| V31-022 | NOT_RUN | V3.1 P2待实施；原官方SDK回环发现/调用回归通过 |
| V31-023 | NOT_RUN | V3.1 P2协议矩阵未完整验收；原拒绝未批准版本回归通过 |
| V31-024 | NOT_RUN | P2待实施；原可信映射回归保留 |
| V31-025 | NOT_RUN | P2新漂移策略未实现 |
| V31-026 | NOT_RUN | P2新目录缓存未实现 |
| V31-027 | NOT_RUN | P2重连/熔断组合未实现；原超时/取消保留 |
| V31-028 | NOT_RUN | 本轮不扩大网络入口；DNS等新矩阵未验收 |
| V31-029 | NOT_RUN | 加密持久Result Store与分页未实现 |
| V31-030 | NOT_RUN | P2持久结果句柄未实现；P1请求内引用不能替代此项 |
| V31-031 | NOT_RUN | 新结果模式协商未实现；原data回归通过 |
| V31-032 | NOT_RUN | P2通用只读缓存未实现 |
| V31-033 | NOT_RUN | P2 single-flight未实现 |
| V31-034 | NOT_RUN | P2完整范围未验收；原输出Schema失败行为回归通过 |
| V31-035 | NOT_RUN | P2完整范围未验收；原副作用未知恢复约束保留 |
| V31-036 | NOT_RUN | P3模型能力画像未实现；本轮不宣称真实窗口能力 |
| V31-037 | NOT_RUN | P3严格结构化Provider选择未实现 |
| V31-038 | NOT_RUN | P3 Provider切换未实现 |
| V31-039 | NOT_RUN | P3切换不重复执行矩阵未实施 |
| V31-040 | NOT_RUN | P3确定性Replay未实现 |
| V31-041 | NOT_RUN | P3 Replay隔离未实现 |
| V31-042 | NOT_RUN | P3 Replay工件未实现 |
| V31-043 | NOT_RUN | P3完整差分报告未实现；本轮有P1固定样本输入比较 |
| V31-044 | NOT_RUN | V3.1 P4最终版本不存在；本轮V3 API/SSE完整回归通过 |
| V31-045 | NOT_RUN | P4整体待验收；原SSE及第三项目Last-Event-ID回归通过 |
| V31-046 | NOT_RUN | P4整体待验收；现有冻结/撤销/历史行为回归通过 |
| V31-047 | NOT_RUN | P4整体待验收；现有Worker恢复回归通过 |
| V31-048 | NOT_RUN | P4整体待验收；现有SkillOps灰度/回滚回归通过 |

## 输入比较与审计修复

执行 `python scripts/context_benchmark.py` 可重现 context-comparison.json：固定虚构输入的UTF-8字节加封装估算由49,155降为1,165，2条证据保留，源结果摘要不变。真实Gateway的大结果测试另外验证18,000字符明细未被改写、只执行一次工具、全文再次超限明确失败。旧/新Mock路径的答案、证据、终态和工具数一致。没有测量真实模型Token、延迟、费用或回答质量提升。

独立复核发现并复现两项问题，已修复：

1. 无选中技能时，ContextSession原先只查任务授权，遗漏来源能力的紧急撤销。现在保存来源能力及原参数，编译和引用读取均复用工具授权，检索来源额外检查其撤销状态；覆盖直接缓存和实际provider链路。
2. 离线Schema合同测试可长时间阻塞。现在spawn隔离，启动后等待10秒，超时结构化FAIL并终止、join、close；真实CLI已验证超时返回。

复核服务未返回完整最终结论；本机已完成这两项修复的专项验证和主审查，不能写“独立审计全部通过”或“没有任何安全问题”。

## 兼容性风险与下一阶段

- 证据去重键增加source_ref/data_as_of，同版不同时点不再覆盖；此类回答的证据数及后续引用编号可能改变。
- 编译开启后会增加授权审计；预算严格时明确失败。任务内缓存不是持久、加密或跨重启Result Store；外部数据新鲜度仍由项目来源负责。
- Skill合同测试增加独立进程开销和10秒限额；直接Python调用需Windows主入口保护。它不是内存/系统资源沙箱，PASS仅代表声明式样本。
- 本机HMAC不是PKI或独立人员审核；未带可信密钥的工件不能视为可信发布。第三项目发布是测试API流程，正式操作沿用skillctl治理。
- 原Store终态会清理calls.result，本轮未改变隐私保留规则。真正的加密大结果存储、MCP网络范围扩展、Provider切换及Replay需要下一阶段先明确范围与验收。
- PostgreSQL、分布式部署、生产运维、企业接口和真实模型均未交付。本轮无默认分支集成授权，main保持原版本。
