# V3.1 首轮实施设计

## 范围与依据

基线是 main 的 ac448dfe820624d4fbfbf1fae66ba05e3b3b426f，规格是 docs/design/V3.0.md 与 V3.1.md。用户已授权：先评估，有待决问题先问，没有问题开始开发。本轮仅 P0 与 P1；V3 已完成能力保留，P2/P3/P4 的新功能不提前实施。Windows Python 3.11、现有锁定依赖、SQLite、本地可信操作者、默认无真实写入不变。

实际 execution_id/completed、partial 对应 task_completed、现有 API/SSE/数据库是兼容基线；设计中的不同命名不用于改写它们。继续使用原 Runtime/Gateway/Store/Worker，不重建运行时。公开路由与数据库无需变更。新实现代码、注释及文档采用中文说明和直观控制流。

## Context Compiler

在 app/context_compiler.py 放内部不可变 ContextItem / CompiledContext 和一次任务生命周期的 ContextSession。它只保存模型输入副本及随机引用，不持有任务执行状态，不取代 Store。引用绑定 tenant/org/user/business/task、发布快照、任务有效期，读取必须调用现有 Gateway.context 重新鉴权。只在本次请求内有效；持久化加密结果存储与跨重启查询是 P2，不伪装为本轮完成。

编译器通过可信设置显式启用（AGENT_CONTEXT_COMPILER=1 或 model_settings.context_compiler=true），默认旧路径不变。启用时在 app/providers.py 的每个模型回合前运行；普通模型和 Mock 共用。无模型的旧 offline 直达工具不需要编译。durable_plan 沿用原显式叶子执行，没有新增模型阶段。

候选项由服务端从系统策略、选中的冻结技能、授权历史、当前请求、当前任务状态及本轮 Gateway 返回值生成。工具内容不能指定 trust_tier 或提升为 system。编译只接受角色与来源相符的消息；拒绝不同主体/任务/快照的项，过期可选项剔除、过期必需项失败。模型调用前重新鉴权，保留现有撤权与取消语义。

固定保留系统策略、当前用户请求、任务状态、选中技能指令和全部本轮原始证据（含 ID、版本、来源、时点与 citation）。历史整条淘汰，不能截断一条假装完整；工具调用及响应成对保留。大工具数据可替换成确定性结果摘要：完整 evidence、原 data.summary（存在时）、受控 context_ref、明确 limitations。不让模型生成新摘要事实。原始结果由 ContextSession 在原有单工具上限内保存，额外提供 read_context_reference(ref) 受控模型工具，读取前鉴权并计入原模型调用总预算；读取的大结果仍受预算，无法容纳则明确失败，不无限重取。

采用保守的 UTF-8 字节数加消息开销作为 token 估算，计入工具描述与输出/安全预留；不得称为真实 tokenizer 计量。真实模型未验证窗口时只使用保守的本地预算，报告来源为 operator_conservative，不能声称核验过 Provider 能力。Mock 标为 deterministic_mock。P3 再引入完整能力画像。原 max_context_bytes 限制仍保留；开启编译时对编译后的输入执行限制。必需项无法容纳返回 CONTEXT_BUDGET_EXCEEDED/422。

编译报告仅保存 ID、digest、数量、预算、估计及省略项；使用现有 store.audit，避免增加旧 SSE 枚举或记录完整 Prompt。已存在 limitations 字段的结构化最终结果合并编译限制说明。保留完整数据的旧任务结果/API 不被摘要替代。

## Skill DevKit

新增 scripts/agent.py 提供 `python scripts/agent.py skill init/validate/test/pack/inspect` 和 `doctor --scope local`，不引入安装器或新依赖。核心实现 app/skill_devkit.py，复用 app.skills 的解析器、Schema 和路径规则，不新建发布库。原 scripts/skillctl.py 发布命令完全保留。

init 不覆盖目录，生成 SKILL.md、兼容 manifest、输入/输出 Schema 和声明式 JSON fixtures。validate 只读校验，所需能力必须出现在操作者提供的 JSON 能力清单；不得从包自述获得批准。版本 3.0、3.1 或无 schema_version 的旧包可加载，未知版本在新 DevKit 明确拒绝，不收紧旧 Registry 加载规则。

test --offline 只执行声明式 Schema 正/负用例，不执行包脚本，不调用网络或模型；输出每条 PASS/FAIL/NOT_RUN 及测试类型 contract，不宣称业务语义评测。第三 Demo 的真实 Gateway 端到端由独立 pytest 覆盖。缺少 fixtures 或 Schema 必须明确 NOT_RUN，不能算通过。

pack 先校验全部文件与依赖，再对内存中同一份字节生成归档：稳定排序、固定 ZipInfo、ZIP_STORED、文件 SHA-256 清单、依赖锁与包摘要。只允许 SKILL.md、manifest、references、schemas、tests 的受限文本文件；拒绝脚本、链接/硬链接/junction、设备文件、路径穿越、大小/数量超限、大小写冲突及明确凭据格式。不把凭据扫描说成任意秘密的完美识别。

inspect 不解压到用户路径、不执行代码，有界读取后核对重复/非法路径、总大小、每文件摘要、工件清单及依赖锁；可使用操作者显式提供的 HMAC 密钥验证候选工件签名。不接受包内自带密钥作为信任。签名是本机 HMAC，不是 PKI。pack 不是发布或安装，篡改工件不能走 inspect 验证，注册发布仍需原 CLI 审核。doctor 只读检查版本/依赖/本地配置状态，不打印密钥也不修改环境。

## 第三 Demo 与验收

examples/v3/demo_c 是纯虚构的集合统计业务，按授权对象聚合数量，提供一个项目适配器与技能，不在核心增加业务分支。独立临时部署后通过原 API/Mock 验证结果、证据、权限拒绝、SSE 重放和幂等；DevKit 对其测试/打包/检查，随后用原 SkillOps 导入与发布。

P0 留下代码映射、API/Schema/状态及 SQL 定义基线、测试清单。P1 回归所有 V3 测试，对新编译路径增加跨租户、撤权、过期、注入、超限、证据时点和大结果测试。固定 Mock 样本对比编译前后结构结果、证据、调用数及估算输入量；真实模型效果与 token 改善写 NOT_RUN。MCP 当前锁定 mcp 2.3.0；本轮不升级 SDK，不扩大 stdio/DNS 网络范围。官方网页核验若不可达，单独记录，不把本地 SDK 测试当成最新线上规范核验。

## 回退和交付

关闭编译开关恢复原模型组装路径；无数据库迁移。DevKit 是附加本地工具，原 SkillOps 可继续使用。完成后提交并上传 codex/v31-p0-p1，保留所有既有分支；本轮没有合并 main 的新授权。交付完整源码、逐项验证结果、风险、P2 准入条件，禁止把首轮完成描述为整个 V3.1 或生产上线完成。
