# P4 验收记录

本记录区分已执行证据与待执行步骤。P4 本机实施已完成，交付于 `codex/v31-p4`，基线 main 为 `ed4f85b`；本轮不合并 main、不发布生产。最终门禁结果须以外部全新证据目录中的 report.json 与原始日志为准。

## 已核验环境和阶段结果

- 独立 Python 3.11.18、SQLite 3.53.1、threadsafety=3；原锁安装与 pip check 通过。原 Python 3.11.2 环境的解释器、venv 配置、SQLite 扩展和 DLL 四项摘要保持。
- 实际 SQLite source id：`2026-05-05 10:34:17 c88b22011a54b4f6fbd149e9f8e4de77658ce58143a1af0e3785e4e6475127e9`；DLL SHA256 `4400b656ff114e6e5513510f10b8757a02268e7e45893f1a6fbb4d77a51a281d`，扩展 SHA256 `59562874c925f20b567fafb8475f35c69081a163c2cad89b2cc8c9b91ab3a6a4`。
- 未改源码的隔离环境基线：597 passed、12 subtests passed，265.35秒，无失败/跳过。
- Task1：先20项失败，包括4项只改变原生SQLite身份却未拒绝旧包；实现后专项48 passed/29.77秒，全量617 passed、12 subtests passed/417.85秒，无跳过。
- Task4 门禁单元：先12项失败；补充源码冻结拒绝后31 passed/3.60秒。限定 runtime_environment/replay/release_check 三模块 mypy 通过，不宣称全库 mypy 零错误。
- Task2：真实HTTP的reactive/durable_plan断线重连通过，完整回滚节点46.91秒通过。
- Task3：6个真实双Worker节点首次通过，68.63秒。Task2/3与既有API/发布/审批/恢复的组合回归147 passed、248.78秒，无跳过。
- 修复前完整门禁：641 passed、12 subtests passed/489.79秒，六项辅助检查通过；独立整分支审查随后确认I1收集跳过/取消选择误READY，故这份报告不作最终就绪凭据。
- I1真实反例先9 failed/1 passed/29.33秒，强化conftest反例1 failed/5.11秒；5735243修复后41 passed/25.44秒与限定3模块mypy通过。仅此一次整分支审查，修复后只复核修复差异；新完整门禁执行结果另由最终证据记录。
- 531e999门禁649 passed/12 subtests/503.35秒，但JUnit对比发现显式tests范围遗漏scenarios/text/test_text.py两项，故不作最终凭据。b49de3f恢复全仓库发现并将两项列必需，新增真实目录外失败夹具先1 failed/3.17秒，再42 passed/48.92秒与mypy通过。最终须以新门禁及完整JUnit集合核对为准。

初始新环境首轮曾出现 MCP malformed_rpc 探针未观察调用，专项及随后完整基线通过，原因未确认；另有硬链接失败/跳过，已最小复现为 Windows AppContainer 与新版 `mkdir(0700)` 的 ACL 继承问题。仅修正测试临时目录方式，原失败日志保留。时长是本机运行记录，不代表性能优化或生产容量。

## 完整门禁的证据映射

| case_id | 层级与实际断言 | 门禁必需测试 |
| --- | --- | --- |
| V31-044 | 契约/本机API；原请求、错误、DB契约与异步行为 | test_v31_compatibility、test_api、test_durable_api |
| V31-045 | 真实回环HTTP；两模式断线重连、持久ID、物理计数与唯一终态 | test_p4_stream_release 两种 execution_mode |
| V31-046 | 本机API与Skill治理；冻结/吊销、历史可见性与实时撤权 | test_skill_governance、P4 rollback |
| V31-047 | 真实双Worker PID；只读恢复及批准模拟写的提交前后/未知/取消 | test_worker_recovery、test_p4_write_recovery |
| V31-048 | 顺序切换候选/基线程序，灰度回滚；保留权威历史、审计、幂等和key；停写backup副本 | P4 rollback |
| P4-SECURITY | SSRF/输入与上下文注入/凭据/技能包投毒/越权撤权/结果与Replay | test_security、high_risk_context、high_risk_mcp、skill_devkit、mcp_result_security、retrieval_revocation、p3_audit、result_store、replay |

完整套件还执行 read_cache/read_cache_integration 等缓存隔离专项，以及 P0～P3 预算/审批/高风险恢复测试。节点缺失、未执行、失败或跳过均不能获得 LOCAL_RELEASE_READY。真实模型 HTTP 回环测试与 Replay MockTransport 测试单列：后者不会连接真实模型或工具。

## 明确未执行和剩余边界

商业模型与供应商能力/驻留/费用验证、企业真实写入、PostgreSQL、跨主机分布式、生产灰度/发布与生产 RPO/RTO 均 NOT_RUN。两进程 SQLite 本机测试不能外推跨主机保证。HMAC 是本地信任；自动脱敏不能发现任意秘密，优先虚构数据并人工复核。

备份副本在保留持久撤权的状态下启动，核对表数据、密钥、历史与HMAC以及结果拒绝访问；受权结果解密的正向路径已在候选和基线程序共享当前权威状态时验证，未在恢复副本上另行解除撤权来重复验证。

原始日志与运行时发行核验资料由本轮交付证据保存。报告摘要用于本机追溯，不是外部不可篡改证明。独立审查发现和修复将按实际结果追加，不能把测试通过解释为绝对无漏洞。

## 最终本机验收

最终门禁提交2f5fc14e0fcac1691ce6da23e24166b8f9b6b796：652 passed、12 subtests passed，门禁测试阶段744.797秒，退出0；所有必需组与六项辅助检查PASS。failed/skipped/deselected/collection_failed/collection_skipped均0；旧641项JUnit节点集合无遗漏，新增11项。

I1已关闭，范围补正复核无新增问题；M1仍是上文明确的非阻断边界。production_status=NOT_RUN。本节及README/计划/账本为门禁后的文档补录；代码和测试没有后续修改。最终远端/源码包一致性见独立交付回执。

## 收尾补验

原 P4 的 M1 只涉及恢复副本缺少成功解密的证据。2026-11-05 的收尾在独立恢复的状态目录中增加正向用例：用仍然有权限的身份读取其结果，逐页比对解密内容与来源，同时保留原撤权拒绝检查，核对物理调用未增加。测试使用虚构数据与本机 SQLite，不对生产备份或灾难恢复时间作保证。
