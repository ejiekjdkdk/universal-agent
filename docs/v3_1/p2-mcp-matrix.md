# P2 MCP本机验证矩阵

依赖固定为mcp==2.3.0，行为依据当前安装SDK源码与本机官方MCPServer测试。不是最新在线协议或企业部署认证。

| 项目 | 状态 | 依据与范围 |
|---|---|---|
| 2026-07-28 Streamable HTTP | PASS | 官方Server，实际TCP回环、显式discover、list_tools、低层session.call_tool |
| 2024-11-05、2025-03-26、2025-06-18 | REJECTED | 配置只批准这些版本时，网关明确MCP_PROTOCOL_INCOMPATIBLE；不自动协商降级 |
| 批准目录与候选CLI | PASS | 真实目录检查后人工写入摘要；原配置不会被CLI修改 |
| 描述/输入Schema/输出Schema/工具集合变化 | PASS | 完整目录摘要变化拒绝执行，不能自动批准新工具 |
| ttl_ms/cache_scope | PASS | 按实际SDK字段解释，平台上限300秒；public仍按主体隔离，未知/零TTL不缓存 |
| 缓存窗口内的完整目录保护 | PASS（强制刷新） | SDK输入Schema摘要不覆盖描述及完整目录，每次物理调用仍刷新比对 |
| 熔断、限流、取消、单探测、主体隔离 | PASS | 可控故障及真实HTTP测试，配置变化重新建范围；取消不计作上游失败 |
| SSRF、DNS、私网、重定向、代理 | REJECTED | 延续仅字面IP策略，回环HTTP须明确允许，公网须HTTPS；不支持DNS与私网 |
| 公网HTTPS生产连接 | NOT_RUN | 无真实服务/企业凭据，仅验证地址策略 |
| 本地stdio | NOT_RUN / REJECTED | 当前配置拒绝stdio，不启动子进程；尚未证明Windows整个子进程树取消/隔离边界 |
| SDK自动重发 | DISABLED | 使用session.call_tool，不使用高层HEADER_MISMATCH重发或补充输入循环 |
| 副作用未知结果 | PASS | 超时后OUTCOME_UNKNOWN且不可重试；不连接真实写系统 |
| 结构与字节限额 | PASS | HTTP/SSE 256KiB，Schema前结构检查，SDK结构化输出校验前也检查数组/深度/节点 |

stdio按已确定的设计边界保持拒绝；不得将该行描述成已支持的本机传输。其他版本的SDK或协议需要另行验证。
