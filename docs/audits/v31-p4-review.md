# P4 整分支独立审查

审查基线：`ed4f85bac11826e2d1bc0814f39a6b1405e6f093` → `0d241ba19797644fdae14ad5b24197e66499872a`。工作树：`C:/Users/Lenovo/.codex/worktrees/v31-p4/universal-agent`。审查依据为提供的完整差异、其中 P4 设计/计划/账本，以及原 V3.1 §9/10/14 和 V31-044～048；聚焦读取原 Store 事务、审批执行、Worker 和旧恢复测试辅助以核对故障切点。没有修改源文件、index、HEAD、分支，没有运行完整套件，没有再派 Agent。

## Strengths

- 产品改动限于 SQLite 原生身份和独立验收 CLI；原 Runtime/Store/Worker/API 未变。SQLite 版本/source id 从真实连接读取，Windows DLL 路径来自已加载模块句柄，原生摘要加入原 Replay 执行器指纹，既有 HMAC/Schema/MockTransport 约束保持。
- 发布检查区别 environment PASS、LOCAL_RELEASE_READY、production NOT_RUN，拒绝旧证据目录、脏源码和源码身份变化，记录完整执行结果与原始检查日志。环境平台与依赖锁检查在完整套件前执行。
- SSE 测试实际关闭 HTTP 响应流，再用持久 ID 重连；有适配器物理探针和终态唯一性断言。回滚测试真正以固定旧源码目录启动新进程，保留当前数据库而非拿旧库掩盖兼容问题。
- 两 Worker 是不同真实 PID，事务包装分别在原 COMMIT 前/后暂停，再杀死实际执行者；原 Store 的事务、审批和账本路径没有替换为模拟实现。提交前独立连接不可见、接手 fencing token 增长、提交后不重发、未知结果与取消均有数据库和 API 断言。
- 文档明确本机虚构数据范围，未把模拟确认丢失说成真实外部业务写入；没有生产、跨机、商业模型或性能收益的夸大声明。

## Issues

### Critical

无。

### Important — I1：收集阶段跳过/取消选择可被误报为完整就绪

- **位置：** `scripts/release_check.py:131`（`SuiteEvidence`），尤其 `137` 的 `pytest_collection_finish` 与 `140` 的 `pytest_runtest_logreport`；入口 `scripts/release_check.py:207` 附近的 pytest 参数与 `262` 的 `_run_suite` 结果消费。
- **触发一：** 不在 REQUIRED_NODES 显式列表中的测试模块使用 `pytest.skip(..., allow_module_level=True)` 或 `pytest.importorskip`。模块跳过产生 collection report，不进入 `session.items`，也不产生普通 runtest report。当前插件因此完全看不到这次跳过。
- **触发二：** pytest 会自动发现源码上级目录的 `pytest.ini`。其中 `addopts = -k ...` 可排除非必需测试；`pytest_collection_finish` 只记录筛选后节点，插件没有 `pytest_deselected` 处理。源码本身可以始终干净，且清理 PYTEST_ 环境变量不影响配置文件。当前 `_run_suite` 未显式选择隔离的受信配置。
- **实际复现：** 使用当前真实 `SuiteEvidence` 和 `summarize_suite`，在修复 SQLite 的同一隔离解释器下分别运行两个最小 pytest 套件。pytest 分别输出 `1 passed, 1 skipped` 和 `1 passed, 1 deselected`，退出码均为 0；两次汇总均为 `status=PASS`、`release_status=LOCAL_RELEASE_READY`、`counts.skipped=0`。通过节点作为必需节点，未执行节点为非必需节点，与完整门禁中的缓存等非逐节点枚举测试具有相同证据流。
- **影响：** 门禁可声称“完整原有及新增测试、无跳过”，实际部分测试未执行。违反 P4 计划的失败关闭条件，不是纯报表显示问题。当前全套恰好没有此触发，不能消除门禁本身的问题。
- **建议：** 捕获 collection skip/fail 和 deselection，并让任一未执行/跳过阻断；固定 pytest 的受信配置与完整测试路径，避免继承上级配置中的筛选参数。选择方案时同时考虑配置的 addopts/testpaths/ignore 类过滤，不能只修最终 counts。增加真实小 pytest 套件回归，而非仅向 summarize_suite 人工传入 SKIP：至少覆盖 module-level skip、配置导致 deselection、成功完整执行；保留现有失败/跳过/源码冻结检查。
- **证据：** `C:/Users/Lenovo/Documents/Codex/2026-11-04/universal-agent-git-2026-11-04/work/p4_review_probe.py`；最终输出 `.../work/p4-review-probe-final.log`。脚本创建并清理自身临时夹具，不修改仓库。首轮扩展探针因重复 pytest 调用的同名模块缓存冲突而失败，原日志 `.../work/p4-review-probe.log` 保留；改成独立模块名后上述三组结果成立。
- **对照排除：** 真实失败 subtest 返回 exit=1、reports=FAIL、BLOCKED；没有发现失败子测试被忽略。对应对照也在最终探针日志中。

### Minor — M1：恢复副本没有受权解密正向断言（已有审查项，接受为非阻断边界）

- **位置：** `tests/test_p4_stream_release.py:355`～`361`；边界已在 `docs/v3_1/p4-acceptance.md:35` 明说。
- 恢复副本保留撤权后只证明历史可读、HMAC/数据保留、结果拒绝访问。403/404 本身不能证明恢复副本在授权条件下能成功解密分页结果。
- **Triage：** 原/旧程序读取同一当前权威库时已经正向验证解密；backup_state 比较两库结构与所有表数据；恢复环境复制密钥/策略且启动成功；文档清楚不声称恢复副本完成正向解密。因此本轮不阻断，也不扩大验收声明。若今后提高备份恢复的承诺，宜为独立授权主体保留一个未撤权结果做正向验证，避免为测试解密而解除应保持的撤权。

## 账本 Rulings 审视

1. **按用户“制定并执行”继续，不重复问计划确认：合理。** 已有明确授权。最初“一次整分支审查、没有逐任务独立审查”的成本记录是历史决定；后续 Task2/3 采用任务级审查的 Ruling 实际覆盖了这一部分，并非当前还缺少那些审查。最终归档时可标注“后续调整”，无需倒改历史。
2. **Bash/MSYS 受限时用中文进度账本与原生命令：合理。** 保留任务/BASE/提交/日志和检查结果，功能上满足追溯；代价是手工同步状态、checkbox 和旧 pending 行。命名对象拒绝的历史复现由父会话提供，本次未独立重演，不将其作为已亲测事实。
3. **Task2/3 分别子 Agent 实施与任务复核，父 Agent 做 Task4：合理。** 两份测试有明确文件归属，Task3 消费已明确的辅助接口；增加两次任务审查成本换取隔离实现上下文和具体恢复语义核对。没有要求再重复广泛整分支审查。
4. **子 Agent 暂存、父 Agent 复制并提交：合理。** 解决的是工具写权限继承限制，不改变功能授权。风险是搬运漂移；固定提交、合并后的组合回归和最终冻结源码门禁是适当补偿。本审查子 Agent 也观察到 git status 的工作树权限限制，父会话确认是同类已知限制；HEAD 完整 SHA 已只读取得。

## Recommendations

完成一次针对 I1 的修复和小范围复核，再在新证据目录运行完整门禁。已在进行的旧 HEAD 门禁可保留为修复前回归证据，不能作为修复后的最终验收。最终更新计划 checkbox、账本最新状态与 acceptance 的“执行中”文字，引用真实新 report/log；本报告不替父会话宣称其尚在运行的完整门禁已通过。

## Declined to judge

- 商业模型、企业真实写、PostgreSQL、跨主机、生产发布/RPO/RTO：用户与计划明确 NOT_RUN，局部本机测试不推导这些保证。
- 恶意本机操作者、篡改后谎报版本的 SQLite、自修改测试/恶意第三方 pytest 插件的完整沙箱安全：CLI 明确执行受信本机程序、摘要不是远程认证；正常 pytest 配置意外筛选仍在审查范围内，已作为 I1。
- 非 Windows 平台独立共享 SQLite 库摘要完整性：本轮 release 目标明确 Windows x64/Python3.11，其他平台被 environment 阻断；不据此宣称跨平台原生来源证明。
- 原始环境保留、发行下载来源、597/617/147 等历史全量结果的独立重跑：本轮接受固定日志位置和阶段报告作为历史证据线索，未重复全套；没有把未亲自运行的历史数字当成本次实测结果。
- 运行中的最终完整 gate：由父会话持有 session 66158；本审查未干预或重复启动，最终完成与退出码仍需父会话核验。
- 全库静态类型与性能/容量提升：计划仅检查三个相关模块并记录本机时长，无全库 mypy 清零或性能目标。

## Assessment

**Ready to merge / declare local readiness: With fixes（当前不可宣称完整本机就绪；本轮也无 main 合并授权）。**

发现 1 项 Important、0 项 Critical；保留 1 项已有非阻断 Minor 并接受其明确边界。接口、原生指纹及真实故障测试没有发现其他确定退化；I1 可使验收遗漏真实未执行测试，需修复并以新 HEAD 的完整门禁证明最终状态。

## 修复与最终核验补录（主流程）

初轮5735243修复收集/过滤漏报并经复核；随后649项门禁与旧JUnit对比揭示tests-only遗漏2项场景测试，初轮全范围假设及完成判断撤回。b49de3f将发现根改为仓库根并加入两场景必需节点，新真实反例先失败后42项专项通过；同一I1范围补正复核ADDRESSED，无新增Critical/Important/Minor。没有再做第二次广泛整分支审查。

最终门禁提交2f5fc14e0fcac1691ce6da23e24166b8f9b6b796：652 passed、12 subtests passed，门禁测试阶段744.797秒，退出0；所有必需组与六项辅助检查PASS。failed/skipped/deselected/collection_failed/collection_skipped均0；旧641项JUnit节点集合无遗漏，新增11项。 I1关闭；M1不扩展承诺。本机通过不代表绝对无漏洞或生产验收。
