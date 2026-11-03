# V3.1 P2 本机使用

本版本继续以V3为开发基准，V4/V5仅为长期愿景。P2默认关闭；依然使用Windows Python3.11、SQLite和锁定依赖。业务场景通过独立项目包接入。

## 加密完整结果

在操作者管理、不会上传的JSON文件中保存以下配置，并将`AGENT_P2_POLICY`设为其绝对路径：

```json
{
  "results": {
    "keys": {"local-v1": "AGENT_RESULT_KEY_V1"},
    "active_key": "local-v1",
    "ttl_seconds": 86400,
    "page_bytes": 32768,
    "max_bytes": 1048576,
    "quota_bytes": 67108864
  },
  "read_cache": {
    "max_entries": 128,
    "tools": {
      "notes/notes_search": {"ttl_s": 10, "freshness": "notes-revision-1"}
    }
  }
}
```

`AGENT_RESULT_KEY_V1`须由操作者用密码学安全随机数生成，值为64位十六进制字符串（256位密钥）。不要复制固定示例密钥、写入源码、日志或分享包。所有API/Worker进程必须配置一致的密钥映射和策略。可以只配置results或read_cache。

新结果默认在任务结束后保留24小时，允许60秒至7天；完成时只设置一次，不因重启或读取延长。执行期间也有保留上限，过期/删除的内容不复活。API和独立Worker每60秒清理到期密文，读取到期即拒绝，不等待清理周期。停机期间无定时器；下次启用服务恢复清理。逻辑删除不保证文件介质物理擦除。

AES-GCM只覆盖Gateway完整工具结果、对应调用缓存和持久计划步骤中的新引用。原任务答案、证据、Mock账本和旧数据库记录保留原语义；这不是整个数据库加密。单结果、深度32、节点65536、数组10000、文本256KiB都有上限，超限明确失败。默认库配额64MiB按新增密文及元数据计，SQLite文件开销不计入此数。

轮换时在keys中同时保留旧key及新key，改变active_key即可。需要恢复历史任务或读取旧结果时必须保留旧密钥；移除旧key会拒绝解密，不能视为“没有缓存”然后重跑工具。关闭P2开关不会把已存在的引用转换为明文，仍需原读取支持或明确让相关任务失败。

## 结果API和模型读取

仅启用results时注册下列接口，仍使用原用户身份认证：

| 接口 | 含义 |
|---|---|
| GET `/api/v1/agent/tasks/{task_id}/results` | 当前所有者的任务结果清单 |
| GET `/api/v1/agent/results/{result_ref}` | 摘要、digest、大小和到期时间 |
| GET `/api/v1/agent/results/{result_ref}/pages?cursor=...` | 有界UTF-8 JSON片段和下一游标 |
| DELETE `/api/v1/agent/results/{result_ref}` | 原所有者提前删除密文 |

按顺序拼接页面content并解析JSON，可还原完整原结果。每次读取重验所有权、对象权限、能力/技能/触发来源和MCP固定配置，句柄和游标都不能授予权限。任务结束后的读取使用任务所有权，不复用已失效的run_context。响应禁止浏览器缓存。

原execute与任务API的data语义保持不变。模型工具消息超过16KiB时改用摘要及受控result_ref，仍保留全部证据，并通过read_result_page读取当前任务内容。最终回答带截短限制说明；模型不能用同一用户的其他任务句柄读取跨任务内容。

## 有时效的只读共享查询

tools中的键是`场景ID/能力ID`。必须同时给出短TTL（大于0，不超过60秒）及操作者维护的freshness。更新数据集时应改变freshness；共享内存不提供外部系统的实时一致性保证。键绑定身份、权限、对象范围、输入、固定/当前版本、快照、技能与MCP映射及凭据摘要。

只合并read，排除写、提案和agent委派。相同授权范围的同时读取可以共用一次物理调用，每个任务仍有独立执行ID、逻辑预算、结果引用与审计；物理尝试由仍获授权的参与者承担。单个等待者取消不取消其他参与者，全部退出会取消查询并释放槽位。每次命中重新鉴权，已完成MCP调用的幂等复用同样检查撤销。共享仅在当前Runtime/进程内，独立Worker之间不合并。

每条缓存结果最多1MiB，默认128条、最多1024条，同时在途查询也受条数上限限制。输出按JSON复制隔离；失败不缓存。MCP共享只在服务已配置P2目录批准策略时启用，防止旧映射绕过目录治理。

## MCP批准目录和健康状态

原`AGENT_MCP_SERVERS`配置不变。需要P2治理时，在指定server下增加：

```json
{
  "p2": {
    "catalog_digest": "用本机检查命令生成并经操作者审核的64位摘要替换",
    "failure_threshold": 3,
    "cooldown_s": 10,
    "max_concurrency": 4,
    "rate_per_minute": 120,
    "cache_ttl_s": 30
  }
}
```

先在保留原可信端点和映射的配置上运行：

```powershell
python scripts/mcpctl.py --config C:/private/mcp.json --server demo --tool lookup --tenant tenant_a --output C:/private/candidate.json
```

该命令只发现目录，不执行工具，也不修改批准配置。人工审核所有工具名称、描述、输入和输出Schema后，将catalog_digest写入可信配置。首次未批准、目录新增/删除、描述或Schema改变都会阻断执行，输出只记录摘要差异。修改批准配置后，已有任务仍受首次使用固定摘要限制，应新建任务。

目录缓存按端点配置、协议、租户、主体和凭据摘要分区，远端public提示也不跨主体。只理解SDK实际返回的ttl_ms/cache_scope，TTL取平台与远端较小值；0或未知提示不缓存。SDK的输入Schema保护不能覆盖整个目录，因此每次实际工具调用仍刷新并比较完整目录；缓存用于已审核快照，不减少这一步网络校验。

健康状态独立于Task状态。暂时故障依次进入degraded/open，冷却后只允许一个half_open探测；取消不计作上游失败。协议不兼容或目录漂移保持拒绝，需可信配置变化后重新审核；disabled由实时配置直接拒绝。每个网关最多256个健康范围，达到上限明确限流，不驱逐熔断状态来绕过限制。已发送写请求的未知结果不会自动重试。

传输支持与拒绝范围见[p2-mcp-matrix.md](p2-mcp-matrix.md)。本轮不提供生产DNS/私网、stdio进程树隔离、PostgreSQL、分布式共享缓存或真实企业联调。
