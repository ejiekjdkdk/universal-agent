# V3 MCP Gateway

已在 Windows / Python 3.11 的隔离虚拟环境中验证官方 `mcp==2.3.0`。测试启动官方 `MCPServer`，通过真正的回环 TCP / Streamable HTTP 调用，不使用 MockTransport 代替协议联通。没有接入真实企业业务。

## SDK 与协议边界

本实现只批准 MCP `2026-07-28`。与此前初始化协议不同，官方 SDK 的 `Client(mode="2026-07-28")` 直接采用版本并生成空能力记录；它本身不会探测服务端。因此网关显式调用 `session.send_discover("2026-07-28")`，检查 `DiscoverResult.supported_versions`，然后采用服务端能力并检查 `tools`。不自动回退到旧协议。

已对照安装的 2.3.0 源码和官方文档验证：

- [官方客户端文档](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/client/index.md)
- [官方传输文档](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/client/transports.md)
- [Streamable HTTP API](https://py.sdk.modelcontextprotocol.io/api/mcp/client/streamable_http/)

2.x 使用 `httpx2` 和 `(read, write)` 两项传输结果。业务原有 HTTPX 不替换为 HTTPX2。官方 `Client.call_tool` 会在 Schema Header 不匹配时重新列举并重发；这里使用 `client.session.call_tool`，避免绕过平台授权检查的隐式调用重试。只读调用重试仍由现有 Runtime 负责；每次协议请求和重连都会再次执行实时授权回调并读取最新允许配置。

## 受信任的部署配置

`AGENT_MCP_SERVERS` 是部署者管理的 JSON 文件绝对路径，格式见 `examples/mcp/servers.example.json`。示例服务默认为禁用。不得把用户可写的文件作为这个配置。配置包含固定端点、明确租户允许列表、每租户凭据环境变量名、每工具动作级别和 Schema、超时及协议允许列表。

场景适配器只有三个字段：

```json
{"type":"mcp","server_id":"demo","tool_id":"lookup"}
```

公开工具名称、描述、权限和 Schema 继续来自审核过的本地场景，远端工具名仅在可信配置中保存。远端 `tools/list` 的描述不会进入模型发现列表，新增远端工具不会自动注册。工具参数不能指定地址、远端工具、身份、凭据或进程。

本地优先版本采取保守网络限制：端点必须为字面 IP，公网必须 HTTPS；开发回环 HTTP 需要显式 `allow_loopback: true`。拒绝 DNS 名、非回环私网 IP、链路本地地址、URL 凭据、查询字符串、fragment 和全部重定向。这样不依赖不确定的 DNS 验证窗口，关闭 DNS 重绑定路径。需要企业 DNS 或私网端点时，应先实现并验证批准 IP 的连接固定，再扩展规则；当前不能宣称支持此类部署。不会读取环境代理、用户 Token 或通用服务 Token，不支持 stdio 或启动任何子进程。

每次调用使用独立 SDK 会话和 HTTP 客户端，不共享租户 Cookie、认证或缓存。`MCPContext` 仅由服务端 Runtime 从持久运行上下文创建，包含主体、租户、运行 ID、Trace ID 和实时权限回调。缺少可信上下文直接拒绝。上游仅收到经过散列的关联标签及绑定到该租户的服务凭据；这些标签用于关联，不替代上游凭据认证。

## 输出、故障与取消

批准配置的本地能力 ID、版本、输入与输出 Schema 必须与任务快照中的场景声明一致；先校验本地输入，再校验服务端输入，SDK 校验声明的远端输出，最后校验平台 Envelope 和本地输出。Schema 不支持外部或内部 `$ref`，避免远端 Schema 解析引起网络访问。所有输出仍是非可信数据，不执行工具结果中包含的提示词、URL 或命令。

响应流和 SSE 事件均限制为 256 KiB；拒绝压缩响应，避免压缩炸弹绕过原始流限制。工具列表限 128 项，不接受分页或重名。整个发现和调用共用 50–30000 ms 的配置期限，取消会传播到 SDK 并关闭传输。

有效结果必须为 `{"data": {...}, "evidence": [...]}`，再交给既有 Runtime 添加执行 ID、版本、审计和统一结果信息。错误不转为成功：未注册工具为 `CAPABILITY_NOT_FOUND`，协议为 `MCP_PROTOCOL_INCOMPATIBLE`，输出为 `TOOL_OUTPUT_INVALID`，只读超时为 `TOOL_TIMEOUT`，非只读发出请求后的超时或断连为 `OUTCOME_UNKNOWN`。非只读不自动重试。远端业务错误只返回固定安全消息，绑定凭据出现在结果中时拒绝传递。

Trace 仅包含批准服务 ID、本地能力 ID/版本、实际协议和持续时间，不包含端点、远端工具名、输入输出全文或凭据。任务首次调用某个服务/工具映射时，由 Runtime 将固定端点、服务策略和工具映射的规范 JSON SHA-256 摘要保存在任务持久层；后续调用和重试都必须匹配这个首次使用摘要，即使逻辑能力版本相同也不能切换端点或映射。新任务可以首次绑定更新后的批准配置。能力/服务禁用仍即时覆盖旧快照并阻止请求；单次调用期间也继续检查完整配置和授权，阻止返回已失效结果。独立 SDK 协议测试可省略持久化回调，生产 Runtime 必须提供它。

## 本地复现

在仓库目录、已安装锁定依赖的环境中：

```powershell
python examples/mcp/server.py
```

服务仅监听 `127.0.0.1:8123`，提供虚构的 `private_lookup`。它还提供仅供测试的 `timeout`、`invalid`、`oversized`、`business_error` 等输入。将示例配置复制到受信任位置，保持其 URL 固定，完成本机验证后启用，并把实际租户 ID 加入允许列表；不要把开发服务器当成生产认证服务器。

```powershell
python -m pytest tests/test_mcp_gateway.py tests/test_catalog.py -q
```

2026-11-03 本机运行通过。测试包含真实 SDK 握手与调用、缺少可信身份、跨租户并发、凭据不透传及输出拒绝、协议允许列表、SSRF/DNS 拒绝、重定向、503、非法协议响应、输出结构与字节上限、业务错误、超时、取消、调用中撤销、非只读未知结果、结果提示词保持数据、能力版本固定、工具名冲突及远端工具缺失、完整 `/chat → Runtime → Catalog → MCP` 集成及原 Catalog 回归。此结论只覆盖本机 SDK 2.3.0 的协议和故障范围，不代表真实企业服务或域名网络部署已经验收。
