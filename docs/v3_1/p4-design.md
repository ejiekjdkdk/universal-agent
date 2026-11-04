# V3.1 P4 本机发布验收设计

日期：2026-11-04。基线 main `ed4f85bac11826e2d1bc0814f39a6b1405e6f093`。本设计落实 `docs/design/V3.1.md` §9、§10、§14 的 P4 和 V31-044～048，以及用户批准的 P4 可行性评估。

## 授权与边界

用户已要求先验证含 SQLite 修复的隔离运行环境，优先保留 Python 3.11 与原环境，再制定并执行增量计划；保留既有功能和接口，真实阻断才提问，实际生产发布不做。GitHub 功能分支上传授权沿用；本轮不自行合并 main。

环境验证完成：Astral python-build-standalone 20261026 的 CPython 3.11.18 / Windows x86_64，实际加载 SQLite 3.53.1、threadsafety=3；发行包 SHA256 为 `67215a7bf1643e33d01b17ee3f1e91a8b0b39163746855ee02594bf0608f68b4`。原依赖锁未改，pip check 通过。未修改产品代码的完整基线为 597 passed、12 subtests passed、265.35 秒，无跳过。原 Python 3.11.2 / SQLite 3.39.4 环境保留，解释器、venv 配置及两个 SQLite 原生文件摘要复核一致。

继续使用原 Runtime、Gateway、Store、Worker、SkillOps 和发布注册表。公开 API、错误码、Task/Step、审批、取消、结果未知与恢复语义不变；partial 沿用 task_completed，不新增 SSE 枚举。仅本机虚构数据、模拟模型 HTTP、真实回环服务和本地模拟账本；商业模型、企业真实写入、PostgreSQL、跨主机与实际生产环境均 NOT_RUN。Replay 仅显式本地测试，不自动录制网页/API。

## 有限的产品改动

新增无业务状态的原生运行时信息模块，供 Replay 与发布检查共享：采集 SQLite 实际版本、source id、线程模式及原生二进制摘要。Replay 把这些字段加入原执行器指纹，原 HMAC、路径/Schema/传输隔离保持。只替换 SQLite 库也必须拒绝旧精确回放包，不能放宽旧校验。

新增独立本机验收 CLI，不注册 HTTP 接口：environment 只报告当前环境是否符合已验证 Windows/Python 3.11/修复 SQLite/原依赖锁；verify 运行完整原有及新增测试并输出结构化证据。environment 的 PASS 不代表完整发布就绪。verify 仅在必需用例实际执行、无失败或跳过时输出 LOCAL_RELEASE_READY；生产状态固定 NOT_RUN。缺失测试、运行中断、错误环境或不可写新证据目录均明确失败。所有测试源码是操作者信任的本地程序，不把 CLI 称为任意不可信测试沙箱。

## 增量验收

- V31-044：原契约快照与完整回归；整理原路由、P2 可选路由、权限和 FE/BE 变更矩阵。
- V31-045：真实 HTTP 流中断与 Last-Event-ID 重连，reactive/durable_plan 各验证物理调用计数、持久 ID、有且只有一个终态；读取不重新调度。
- V31-046/048：复用既有场景/Skill 灰度、冻结和吊销专项，再做真实 API 进程重启、场景灰度回滚以及候选/基线代码目录的顺序切换。共享当前权威状态而非恢复旧库；保留历史任务、事件、审计、幂等绑定、结果密钥与操作者 HMAC。权限缩小继续拒绝，过期/删除结果不复活。
- V31-047：现有真实双 Worker 只读恢复继续运行；新增审批模拟写入的双 PID 竞争、提交前崩溃、提交后崩溃、丢确认和取消。通过测试进程中的事务包装/屏障定位故障，不向生产代码加入测试 Hook。断言对象版本、回执、调用与终态唯一；未提交只读步骤仍允许重试，未知写结果禁止重发。
- 安全：现有 SSRF/注入/包投毒/跨租户与缓存/结果/凭据/预算专项统一列证据；独立全分支审查；确定问题先复现再修复。

## 回退和记录

代码回退基线是 ed4f85b，先在同一修复运行时通过回归；V3 契约快照的 ac448df 不等于任意数据的可用回退程序。旧新代码顺序停启，不默认同时写活动库。正常代码回退保留当前状态；备份恢复演练另在停写的虚构数据副本进行，使用 SQLite backup API，并保持任务库、Skill 库、快照/注册表、密钥和可信配置一致。不得仅复制活动 .db 或丢弃 WAL，不能移除读取既有加密引用所需的配置和旧 key。

每条证据带 case_id、level、commit/tree、运行时/夹具摘要、命令、断言、状态和日志。原始失败日志保留。Windows 沙箱测试需短目录；CPython 3.11.10+ 的 mkdir(0700) ACL 与 AppContainer 硬链接权限已最小复现，验证工具只在沙箱自身 Temp 使用继承 ACL 的测试目录，产品权限与原断言不变。

## 来源

- SQLite WAL-reset 修复与 WAL 持久状态：https://sqlite.org/wal.html#walreset
- 运行时发行版：https://github.com/astral-sh/python-build-standalone/releases/tag/20261026
- Python Windows mkdir(0700) 变更：https://docs.python.org/3.11/library/os.html#os.mkdir
- 原审批与发布/恢复约束：`docs/v3_1/compatibility-baseline.md`、`docs/v3_1/p2-usage.md`、`docs/v3_1/p3-acceptance.md`。
