# V3.1 P2 本机验收

基线为main@af952ae，工作分支codex/v31-p2。P0/P1和V3原功能保留。完整设计与例外见[p2-design.md](p2-design.md)，使用方法见[p2-usage.md](p2-usage.md)。本记录不代表企业生产部署完成。

## 结果

最终审查修复后全量测试：**505 passed、12 subtests passed，186.28秒**。命令`python -m pytest -q --tb=short`，Windows本机允许权限执行；完整日志随交付包保存为V3.1-P2最终回归日志.txt。

- 新工作树基线：430 passed、12 subtests，164.92秒。
- 加密结果及原执行恢复：72 passed，38.72秒。
- 只读缓存/权限/预算/原技能及专业处理器：45 passed，15.91秒。
- MCP、CLI、协议矩阵、结果及Worker组合：79 passed，40.98秒。
- 修复前完整实现：499 passed、12 subtests，204.55秒；该数字不能代替审查修复后的最终回归。
- 独立审查后的专项：70 passed，24.78秒；真实快照门禁与恢复/评测补验23 passed，7.45秒。
- 四个模块`app/result_store.py`、`app/result_routes.py`、`app/read_cache.py`、`app/mcp_gateway.py`执行`mypy --follow-imports=skip --ignore-missing-imports --check-untyped-defs`通过。不是全库类型零错误声明。
- `compileall -q app scripts examples`、两个网页脚本`node --check`、`pip check`、Git差异空白检查通过；无新增第三方依赖。

## TDD与独立审查

独立审查固定评价d3dd1cd，发现3个Important，原结论为不能交付。执行者在同一修复轮次内复现并修复：

1. 调用已封存但步骤保存前引用被删除，可能回退明文。现在从权威completed call读取内部标记，缺失引用/关闭读取支持明确失败，后继不执行。删除和停用两种测试先失败后通过。
2. MCP凭据移除后，完成调用和结果API仍可复用。现在所有复用入口重新检查凭据存在性/合法性；P2服务的固定摘要包含凭据范围摘要，轮换后旧结果拒绝。移除、异常凭据及轮换测试先失败后通过。
3. 已知目录漂移进入incompatible后共享缓存仍命中。现在缓存、幂等调用及结果读取统一检查该范围的已知阻断状态。实际发布快照共用Runtime的MCP网关，不能用独立网关实例绕过状态。漂移场景及快照路径均先失败后通过。

另在实施中修复了已完成MCP调用漏检配置撤销、SDK结构化输出Schema前缺少结构限制、空闲时缺少到期密文清理。都保留对应回归测试。没有再次派审查员并冒称“重新审查通过”；最终修复证据来自失败复现、专项和最终全量。

## 设计验收映射

| 编号 | 状态 | 测试及范围 |
|---|---|---|
| V31-022 | PASS（HTTP） | test_mcp_gateway实际官方SDK Server TCP回环发现/调用；stdio单列NOT_RUN |
| V31-023 | PASS | test_mcp_p2的2024/2025版本明确拒绝；只批准2026-07-28 |
| V31-024 | PASS | 本地映射限制、目录新增摘要变化、未注册远端工具拒绝 |
| V31-025 | PASS | test_mcp_p2目录候选批准/字段变化摘要；test_mcp_result_security已知漂移阻止全部复用 |
| V31-026 | PASS | 目录TTL、public提示仍按主体隔离、MCP服务凭据隔离 |
| V31-027 | PASS | 超时、重连、限流、单探测、取消清理、incompatible不按时间自愈 |
| V31-028 | PASS | 字面IP/回环白名单；DNS、私网、重定向、代理策略维持拒绝 |
| V31-029 | PASS | test_result_store和test_result_integration：AES-GCM、AAD、大小限制、分页重组、模型摘要及完整原始data |
| V31-030 | PASS | 所有者/跨主体、TTL、删除、权限/能力/MCP凭据撤销，重启和旧key读取 |
| V31-031 | PASS | 默认OpenAPI快照与旧API完整结果；仅启用P2时增加结果路由 |
| V31-032 | PASS | test_read_cache_integration：输入、身份、版本、权限、对象、技能、freshness；MCP映射/凭据范围 |
| V31-033 | PASS | 10任务单物理读取/独立执行ID，单槽无死锁，单调用者取消、全部取消和独立预算 |
| V31-034 | PASS | 原Catalog输出Schema拒绝、MCP坏输出/结构限额、失败不缓存 |
| V31-035 | PASS | 实际已发送模拟副作用超时OUTCOME_UNKNOWN且retryable=false，未接真实写系统 |

P0/P1的001～021原测试随全库回归；V31-020仍保留原工件安装流程未实际执行的缺口，不能因本轮测试多而改写为已安装。P3/P4、PostgreSQL、真实多模型专业Agent均不在本轮实施范围。

## 兼容性与限制

- P2默认关闭，旧客户端及Mock账本路径保留。新增加密引用任务依赖原密钥/配置，停用不能退回明文或重执行已完成工具。
- 启用P2 MCP服务后，凭据轮换会使旧任务与已保存结果拒绝读取；应按所需保留期安排轮换或先读取必要结果。
- 目录完整摘要保护采取每次物理调用强制刷新，缓存不会跳过这一步。只读结果可在操作者指定的短TTL内复用，freshness须随数据版本维护。
- 健康与目录快照、只读合并均为进程内状态；不同进程不共享健康观察。需要跨进程持续撤销时由操作者修改可信enabled/批准配置，所有读取路径实时检查。重启后实际调用仍要重新发现并比较批准目录。
- stdio尚未完成Windows整个子进程树的隔离/回收证明，保持拒绝并记录NOT_RUN。无真实公网/企业MCP联调。
- AES-GCM仅覆盖新完整工具结果和对应持久引用，原答案、证据、Mock账本不属于整库加密承诺。逻辑删除不保证介质物理擦除。

## 审查裁定与交付

审查员暂不评价的stdio、旧协议真实互通、企业公网/DNS/私网、分布式缓存/PostgreSQL/Provider Router/Replay、整库加密/介质擦除均属于设计已明确排除或未验收范围，继续保留相应限制；没有因此跳过新增步骤明文回退的修复。审查员未评价公开扫描与上传，由执行者另行核验。无待处理Minor项。

用户授权：完成并验收后将仓库从私有改为公开，保留现有分支。本轮P2仅上传功能分支；main仍保留P0/P1基线，合并需要后续明确指令。公开前历史扫描和实际远端操作记录在交付报告中，不把计划写成已完成。

实际上传、公开及分支核验已完成，见[p2-delivery.md](p2-delivery.md)。
