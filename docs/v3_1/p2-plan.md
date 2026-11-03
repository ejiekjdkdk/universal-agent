# V3.1 P2 实施计划

> **For agentic workers:** 使用superpowers:executing-plans在当前会话逐项实施，完成后独立全分支复核。用户已授权无阻断直接开发，不重复请求相同范围批准。

**Goal:** 在保留V3/P1的前提下，实现受控MCP目录/健康、加密结果和只读去重。

**Architecture:** 原Runtime/Gateway/Store/PlanWorker仍是唯一执行链。ResultStore与只读协调器只处理已授权结果，不授予执行权限，不新增任务运行时。

**Tech Stack:** Windows Python3.11，SQLite，锁定mcp2.3.0及cryptography50.0.2，无新增依赖。

**Spec:** docs/v3_1/p2-design.md；docs/design/V3.1.md。

## Global Constraints

- main@af952ae为兼容基线；已有API、错误及Store/Worker租约恢复语义保留，新增结果路由默认关闭。
- 中文注释/Docstring、少封装与嵌套；不改核心业务分支，不接企业系统，不执行真实写入。
- 新结果库默认保留24小时可配置；旧模式清理不变。无key拒绝、错误key拒绝，不能重置数据库或盲重放。
- Git分支codex/v31-p2，保留main及原分支，仅上传功能分支，不自动合并。

## Review Focus

- 加密结果同事务落库与call完成，Worker租约丢失/进程中断不能产生可被另一主体读取的孤儿句柄。
- 单个等待者取消不能取消其他获授权等待者，实际调用和各任务的逻辑调用预算均不能漏记。
- 服务描述/Schema漂移、MCP配置或身份变更时，缓存不能绕过实时授权。
- 缺失key、TTL到期、显式删除后，已完成工具不会通过恢复或分页再次执行。
- 原始明细不能残留在新增日志/调用缓存/步骤输出；旧答案和证据的既有留存单独说明。

## Task 1：加密结果存储及基础边界

Files: app/result_store.py；tests/test_result_store.py。

Interfaces: ResultStore(store, settings)共享原SQLite连接；put(task_id,result,provenance)→随机ref；load(ref)→内部已验证记录；metadata(ref,actor,authorize)；page(ref,cursor,actor,authorize)；revoke(ref,actor,authorize)；purge()。所有外部读取先检查owner并调用可信授权回调，解密只在内部授权后。私有内部解码供原Store恢复使用，不注册为模型工具。

- [x] 写AES-GCM密文、跨行AAD篡改、跨主体、TTL、游标篡改、删除、配额、重启与key轮换测试，运行观察缺失功能失败。
- [x] 在新模块内创建增量表，使用现有事务/锁；密钥环境引用、默认86400秒、单结果1MiB/深度32/节点65536/数组10000/文本256KiB，页32KiB、库64MiB；检查后写入，不静默截断。
- [x] metadata/page返回no raw storage URL、固定digest、UTF-8片段与签名游标；到期与删除无明文返回，清理不保证介质物理擦除。
- [x] 专项通过，提交。

## Task 2：原执行链和受控结果API

Files: app/runtime.py、store.py、plan_store.py、main.py、providers.py、context_compiler.py；app/result_routes.py；tests/test_result_integration.py、test_v31_compatibility.py。

Interfaces: Runtime沿用原构造签名，从settings.p2或AGENT_P2_POLICY可信JSON加载；Store通过私有_persist_tool_result/_restore_tool_result接入，不改已有公共方法参数。新增路由由独立install_result_routes(app,runtime,identify)注册；默认关闭时旧OpenAPI快照不变。

- [x] 先写旧execute完整data、完成后owner分页、工具/技能/触发撤销、与live run失效分离、重启读取和默认关闭API回归。
- [x] 写Durable成功调用恢复、前驱结果解析、聚合、旧明文步骤兼容、丢key或过期不重执行、失租不落结果测试。
- [x] ResultStore写入与complete_call同事务；调用缓存和步骤output仅存内部引用，原路径读取还原完整结果；终态按已确认TTL保留加密行，旧calls.result仍清理。新引用缺失时明确失败，不解释为无缓存。
- [x] 新GET任务结果清单/摘要/分页及DELETE原owner删除，逐页重验权限与版本；不改变旧Task响应。P1模型投影保留完整证据及受控分页入口。
- [x] 专项和原Worker/权限/API/P1回归通过，提交。

## Task 3：有时效的只读缓存与single-flight

Files: app/read_cache.py；app/runtime.py；tests/test_read_cache.py、test_read_cache_integration.py。

Interfaces: ReadCache.run(key,participant,operation)→已验证输出；participant带可信guard和预算回调，operation接收共享guard。key由Gateway从身份/权限/对象/版本/参数/时效/技能/MCP映射构造，模型不能直接给key。

- [x] 先写同参数同权限一次物理读取、每任务独立执行ID/审计、不同主体/版本/freshness不合并、命中撤权拒绝、输出深拷贝、失败不缓存、写/agent不缓存。
- [x] 写单个取消/超时、全部取消、首调用者撤权、额度耗尽、异常后无悬挂flight测试。
- [x] 仅可信逐工具策略有TTL和freshness时启用，有界容量/时间；不跨进程合并。保留原Runtime有限重试与并发槽，所有退出路径回收共享资源。
- [x] 专项和预算/Worker回归通过，提交。

## Task 4：MCP目录、漂移、健康和协议矩阵

Files: app/mcp_gateway.py；scripts/mcpctl.py；examples/mcp；tests/test_mcp_p2.py；docs/v3_1/p2-mcp-matrix.md。

Interfaces: 扩展受信任可选策略与候选目录检查命令，原MCPGateway.execute不变；health状态不进入Task枚举。目录缓存与协议摘要只使用安装SDK实证支持的接口。

- [x] 先写批准目录摘要、描述/Schema漂移、新工具拒绝、TTL/cacheScope、主体隔离、缓存撤销和映射变化测试。
- [x] 写失败阈值/熔断/单探测/取消释放/限流、disabled和incompatible不自动恢复、写入未知不重试测试。
- [x] 实现有界缓存、可信摘要及候选目录/差异日志；无法证明协议摘要保护时强制调用前刷新。
- [x] 用真实官方SDK Server验证HTTP；本地stdio按设计边界保持拒绝并记录NOT_RUN，2024/2025版本明确拒绝。网络策略保持DNS/私网/重定向拒绝，不扩大企业接入。
- [x] 记录实际支持/拒绝/未验证矩阵；专项通过提交。

## Task 5：整体验收与交付

Files: docs/v3_1/p2-{acceptance,usage,progress}.md；README.md；.env.example。

- [x] V31-022～035逐项记录PASS/FAIL/NOT_RUN，原001～021回归不覆盖遗留安装器缺口。
- [x] 全量pytest，受改模块适用类型检查、compileall、node --check、pip check和差异空白检查。
- [x] 独立全分支审查，重要问题复现修复；说明任何未返回的审查结论，不能冒充完成。
- [ ] 提交并上传codex/v31-p2，验证private及main未变；完整源码包、验收日志与更新交接放聊天outputs。
