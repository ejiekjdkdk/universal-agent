# P2 执行记录 — docs/v3_1/p2-plan.md

- 用户确认：显式启用后的加密完整结果，任务结束后默认保留24小时，可配置；旧模式清理不变。
- 基线：main及P0/P1分支af952ae，已在main验证430 passed、12 subtests，147.87秒；新P2工作树430 passed、12 subtests，164.92秒。
- 隔离工作树：C:/Users/Lenovo/.codex/worktrees/v31-p2/universal-agent；分支codex/v31-p2。旧工作树与main未改动。
- 设计检查v31_p2_builder_check：nothing important left，无额外用户决策阻断。工程边界按p2-design.md实施。
- 官方网页查询连接失败；将核验安装SDK源码并保留真实本机协议测试证据，不声称最新在线协议认证。
- Pre-flight: Task1结果库共享Store事务，Task2负责原调用/步骤持久副本解码，不改变Gateway返回data；Task3只共享已验证原输出，每个任务独立存储；Task4网络策略及配置摘要进入Task3缓存键，不能只按参数缓存。
- Ruling: 原Windows工作流使用仓库内p2-progress.md记录执行，不依赖额外shell任务脚本；该文件与Git提交作为连续执行依据。
- Task 1: complete — 新结果库先12项因模块缺失失败，完成后12 passed；增加真实数据库重开、AAD、配额及完成时起算保留期，最后1项先失败再修复。结果库与原兼容快照17 passed，3.47秒。AES-GCM、受控分页/删除、密钥轮换读取和同事务回滚已验证。
- Ruling: 保留期以任务终态时间起算，finish_task重新加密认证到期时间；已过期/删除的结果不复活。步骤/调用持久引用适配在Task2，当前没有改变原运行路径。
- Task 2: complete — 结果集成、恢复、模型分页、真实PlanWorker及原兼容专项72 passed，38.72秒；命令：python -m pytest -q tests/test_result_store.py tests/test_result_integration.py tests/test_result_recovery.py tests/test_plan_store.py tests/test_worker_recovery.py tests/test_approval_recovery.py tests/test_context_compiler.py tests/test_context_integration.py tests/test_v31_compatibility.py --tb=short。新增模块mypy 2项通过，compileall通过。日志保存在聊天work/v31-p2-results.log。
- Ruling: 仅Gateway工具结果进入新加密库；旧Mock账本及任务答案/证据沿用原保留规则，不宣称整库加密。Catalog增加可选输出限额回调，在本地Schema验证前检查；MCP远端SDK边界留给Task4。
- Ruling: 终态TTL只初始化一次，Store重启恢复后补记原finished_at，重复读取/重启不延长；缺失引用明确失败不重执行。模型投影截短时最终回答保留limitations。
- 用户最新分享授权：P2完成并验收后，将GitHub仓库从private改为public，任何人可访问；公开前检查上传内容和历史中的凭据风险。尚未修改可见性，也未授权本轮P2自动合并main。
- Task 3: complete — 缺少模块先失败，基础7 passed；真实Gateway先3失败，集成后通过；扩展预算/身份/时效/能力/参数/对象范围回归45 passed，15.91秒。MCP接入后的组合回归61 passed，13.12秒。read_cache模块mypy通过。
- Task 4: complete（本机HTTP范围）— 目录、CLI、协议拒绝、健康策略、真实MCP共享查询以及结果/Worker集成79 passed，40.98秒；日志work/v31-p2-integration.log。SDK结构化大数组先复现进入Schema校验，再增加前置限额后通过。重复已完成MCP调用绕过远端配置撤销先复现，再补实时映射检查后通过。
- Ruling: Task3/4一起提交其余代码，因为MCP映射与共享缓存的实时授权需要同时接入；保留前两阶段提交不重写历史。
- Ruling: SDK只保护输入Schema，目录快照虽按ttl_ms/cache_scope有界分区存储，但每次物理调用仍强制刷新完整目录。代价是保留一次目录网络请求，以免描述/输出Schema变化绕过批准。
- Ruling: stdio按p2-design.md允许的边界保持拒绝并记录NOT_RUN；尚无Windows整个子进程树隔离/回收证据，不声称支持。2024/2025协议明确拒绝而非自动批准降级；真实企业公网HTTPS未联调。
- Ruling: 健康状态最多256个配置/主体范围，达到上限明确限流，不驱逐活跃熔断状态以免绕过保护；代价是长期频繁配置/身份轮换后需受控重启。
- 补充：API和独立Worker每60秒清理到期密文，停机前取消维护任务再关库；新增空闲清理测试先失败后通过。四个新增/受改模块mypy通过。
- Task 5: in_progress — 首轮全量493 passed、12 subtests，176.29秒；该轮启动后追加的清理/CLI测试已专项通过，仍需最终全量、独立审查、上传和公开前历史扫描。日志work/v31-p2-full.log。

- Final: fixed 加密引用删除后步骤回退明文 — test_result_removed_between_call_and_settlement_never_falls_back_to_plaintext两场景RED→GREEN；completed call内部引用为权威，缺失/停用不回退。
- Final: fixed MCP凭据撤销/轮换后仍复用结果 — test_all_reuse_paths_recheck_credentials_and_known_drift移除/异常/轮换RED→GREEN；P2固定摘要含凭据范围，原值不持久化。
- Final: fixed 已知目录漂移仍命中共享缓存 — 同测试drift RED→GREEN；真实发布快照路径补验先失败再共享Runtime网关状态；缓存、幂等、metadata/pages统一门禁。
- Final: Ruling: 接受审查3个Important并全部修复；没有再次派全分支审查员，原审查Ready No仅评价d3dd1cd，修复结论依据复现及最终回归。审查未评价的明确排除范围继续按p2-acceptance.md列明，公开扫描/上传由执行者另行核验。
- Final: Ruling: MCP健康观察与目录缓存是进程内状态，快照共用当前进程网关；跨进程持续撤销由操作者修改可信enabled/批准配置，代价是不提供分布式健康一致性，实际调用仍每次刷新目录。凭据轮换使P2旧结果失效是安全边界而非静默换身份。
- Final: minor (deferred): 无。
- Task 5 local acceptance: complete — 505 passed、12 subtests，186.28秒；四模块mypy、compileall、node --check（app/review）、pip check、差异检查通过。日志work/v31-p2-after-review.log。最终上传与公开待执行，main仍af952ae。

- Task 5: complete — 最终代码8e39dcc已推送origin/codex/v31-p2；2026-11-03按用户明确授权公开仓库，连接器/API确认public，匿名读取验收HTTP200。main保持af952ae，原分支全保留，无PR/合并/强推。完整Git源码zip、最终日志、审查报告和交接已保存聊天outputs；交付文档随后单独提交。
- 公开前复扫：652对象/413blob/3720287字节，6处均为已人工确认的拒绝测试虚构样例，无敏感数据文件；无新增命中。没有把规则扫描等同绝对无秘密保证。

- 后续main集成：2026-11-03用户明确要求将P2开发与审查修复合入main并推送，保留全部功能分支。main由af952ae快进至20c4a8b，功能代码无额外改动；main工作树完整回归505 passed、12 subtests，204.61秒。同步更新交付文档，原codex/v31-p2与其他工作树保留。最终远端状态见聊天合并验收记录。
