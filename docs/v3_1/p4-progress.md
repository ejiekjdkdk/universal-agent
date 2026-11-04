# P4 执行账本 — plan: docs/v3_1/p4-plan.md

- 基线 main ed4f85b，独立工作树 `C:/Users/Lenovo/.codex/worktrees/v31-p4/universal-agent`，分支 codex/v31-p4。
- 用户明确授权按评估实施，先运行时再增量计划；优先 Python3.11、原环境保留，真实阻断才问，生产发布不做。本轮不合并 main。
- Native runtime: CPython3.11.18 / SQLite3.53.1，实际 source id、DLL/扩展摘要和发行来源已存当前会话 work/p4-runtime；原锁安装，pip check PASS；原环境四文件摘要未变。
- 基线首次 594 passed/2 failed/1 skipped/12 subtests，403.85秒：MCP malformed_rpc 首次未观察 tools/call；专项已通过，原因未确认。硬链接失败/跳过已最小复现为新 CPython mkdir(0700) Windows ACL 与 AppContainer 继承不兼容。
- 验证辅助脚本只使用沙箱私有 Temp 的短目录和默认继承 ACL，保留原断言与产品权限。5项专项通过6.98秒；再次完整597 passed、12 subtests passed、265.35秒，无跳过。
- Ruling: 用户已明确“制定并执行”，采用当前会话原生执行，不重复请求计划确认；一次独立整分支审查。代价：没有逐任务独立审查，以 TDD/专项和最终审查补偿。
- Ruling: 技能的执行账本 Bash 辅助脚本依赖 MSYS shell，此机沙箱此前已确认拒绝其命名对象；使用本仓库中文进度账本和原生命令等价记录任务/BASE/验证。代价：需手工核对任务完整性，提交与日志均可追溯。
- Pre-flight: Task1 sqlite_runtime 输出供 Replay/环境门禁使用，Task4复用，不在 HTTP 请求暴露或添加权限。
- Pre-flight: Task2进程辅助函数供 Task3复用；测试屏障只在测试启动脚本，不进入 Store/Worker；接口一致。
- Pre-flight: Task4必需节点依赖Task2/3测试名；缺失即阻断，不从测试代码存在推导PASS。
- Task 1: complete。20项先失败（包含4项 SQLite-only 变化未拒绝）；实现后专项48 passed/29.77秒，新增2模块限定mypy通过；全量617 passed、12 subtests passed/417.85秒，退出0，无跳过。日志p4-task1-red/green/full.log。SQLite身份进入原Replay指纹；独立environment命令只认环境通过，完整发布/生产仍NOT_RUN。
- Task 4预检待补：空/损坏/重复依赖锁和缺必需验收节点的拒绝；environment阶段的通过不能成为完整就绪声明。
- Task 2: pending。
- Task 3: pending。
- Task 4: pending。

- Ruling: Task1完成后，独立的Task2/3验收测试采用新子Agent实施和任务复核，主Agent继续Task4；控制共享文件归属并保留最终整分支审查。代价：增加两个任务级审查，但减少长会话实现上下文相互影响。
- Task 2: in_progress，BASE=7d0b77e；简报/报告在当前会话work/p4-task2-*。
- Task 4: in_progress。新增空/损坏/重复锁、缺必需节点、跳过/失败/中断、已有目录、宿主.env与源码变化拒绝测试。先12项失败，再31 passed/3.60秒；限定3模块mypy通过。完整门禁等待Task2/3实际节点，当前不声明发布就绪。
- Ruling: 子Agent不能继承父会话工作树写权限时，在会话work暂存，由主Agent复制并提交；不重复向用户索取任务确认，不扩大功能范围。
- Task 2: complete，提交0a6e14e。真实SSE两模式通过；rollback最终单节点46.91秒通过。先前失败为夹具技能内部检索绑定、探针改动受信adapter摘要、终态calls缓存已清空三类断言/夹具不符，按原实现修正，不改产品或放宽发布门禁。
- Task 3: complete，提交18c5d71。6个真实双PID节点68.63秒通过；正常/提交前/提交后、未知结果、取消零写及已提交回执保留均有原API/数据库断言。
- Task 2/3组合回归：147 passed，248.78秒，退出0，无跳过，p4-task23-regression.log；两个新文件与既有API/发布/审批/恢复共同运行。
- Task 3任务复核：Spec/Quality通过，无Critical/Important/Minor；只读核查原Store事务和进程辅助，未重复运行测试。
- Task 2任务复核：Spec/Quality通过；M1恢复副本未做受权解密正向路径，不阻断，边界已写acceptance。
- 整分支独立审查ed4f85b..0d241ba：1 Important I1（collection skip/deselection漏报可能误READY），0 Critical；真实小pytest复现。完整报告docs/audits/v31-p4-review.md。
- 修复前门禁0d241ba：641 passed、12 subtests passed/489.79秒，六项辅助检查通过，退出0。此版本门禁有已确认I1，报告的READY不作为最终交付凭据，修复后必须新目录完整重跑。
- Ruling: M1按审查结论作为明确非阻断边界保留。代价：恢复副本受权结果解密缺直接API正向证据；本轮只承诺同表/密钥/HMAC/历史及撤权拒绝，当前权威库的候选/旧程序正向解密已覆盖。
- Ruling: 收尾沿用用户持续GitHub上传授权及本计划的功能分支交付，不因技能菜单重复询问；保留分支/工作树，不合并main或部署。代价：若用户另有集成目标需后续明确指令，本轮不能把P4说成仓库默认分支内容。
- 最终审查唯一修复波次I1：5735243。真实小pytest先9 failed/1 passed/29.33秒；强化上级conftest反例1 failed/5.11秒；修复后环境/门禁41 passed/25.44秒，限定3模块mypy通过。显式tests范围、专用空配置和confcutdir隔离上级过滤，收集skip/fail/deselection均入阻断计数；不创建生产接口或修改原测试门槛。修复差异复核及新完整门禁待结束。
- 计数交叉核验发现I1尚未完全闭合：531e999门禁649 passed/12 subtests/503.35秒、辅助检查通过，但原641项加新增10应为651；JUnit精确差分缺scenarios/text/test_text.py的2项。先前“全部测试在tests”的假设错误，该轮READY不作最终凭据；恢复显式全仓库发现并加入场景必需节点及真实夹具回归，保留上级配置隔离。
- Ruling: 已确认的I1范围残余继续在原缺陷修复中处理，不以一次修复流程的形式限制交付正确性。代价：增加一次范围复核和全量门禁；不新增业务功能或另做广泛整分支审查。
- I1范围补正b49de3f：仓库根发现，保留上级配置隔离，P4-SCENARIOS明确两既有场景必需节点。新场景目录真实失败夹具先1 failed/3.17秒，再全部42 passed/48.92秒，三模块mypy通过；最终完整预期652项+12子测试，还需核对实际JUnit集合。

- 最终状态：Task1～4本机实施完成。最终门禁提交2f5fc14e0fcac1691ce6da23e24166b8f9b6b796：652 passed、12 subtests passed，门禁测试阶段744.797秒，退出0；所有必需组与六项辅助检查PASS。failed/skipped/deselected/collection_failed/collection_skipped均0；旧641项JUnit节点集合无遗漏，新增11项。
- 整分支审查仅一次；I1经反例修复及范围补正，修复差异复核均通过，无遗留已确认Critical/Important；M1保留明确边界。早期两个门禁READY均不作最终凭据。
- Ruling: Git CLI认证辅助器受沙箱限制，沿用认证GitHub连接器发布同树提交，本地增量历史保留到codex/v31-p4-local。代价：远端发布提交元数据和SHA不同于本地原增量提交，须核对Git树完全一致；不强推main或P3。
- 最终门禁后仅补文档；产品/测试/锁/静态资源相对门禁提交保持。远端提交、树和源码/证据包摘要由外部交付回执记录，不在本提交内自证上传结果。
