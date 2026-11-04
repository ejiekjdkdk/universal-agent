# P3 使用说明

P3 在 V3.1 P0/P1/P2 基础上增加可信模型路由和显式本地 Replay。没有配置 provider_policy 时保留原模型和 Mock 路径；没有自动录制日常网页/API任务的开关。公开 API、SSE、数据库结构和 durable_plan 执行协议未增加字段。

## 模型路由

复制 `examples/v3_1/p3/provider-policy.example.json` 到本地可信配置目录。示例全部禁用且已过期，不会调用任何模型。通过 `AGENT_PROVIDER_POLICY` 指向配置文件，或在嵌入式启动时传入 `model_settings.provider_policy`。关闭时删除两处配置。

操作者需填入真实验证过的画像、验证证据摘要和有效期；不要仅修改过期时间就声称完成验证。`mock` 只允许回环服务，`operator_verified` 是操作者声明，系统不会独立认证第三方能力、数据驻留或保存行为。所有商业模型测试在本轮均为 NOT_RUN。

`routes` 按业务 ID 定义有序候选、数据类别、区域与保存策略允许列表，以及请求、Token、成本和结果年龄预算。data_class 必须保守覆盖整个业务出站数据，包括输入、历史、技能与工具结果。未验证的严格输出/工具能力不可用；启用 stream 时还须验证流能力。凭据通过 `credential_env` 引用本机环境变量，不能写进配置或仓库。

每次发送重新鉴权和核对冻结配置；中途变更配置或凭据会终止旧任务。429/503/408、超时、连接失败、不完整流允许有界切换。已成功工具按规范化参数复用并重新鉴权；过期或 freshness 改变会失败，不为刷新数据重执行。状态未知的副作用恢复仍由原 durable_plan 负责。

P3 固定使用 Chat Completions 的 strict json_schema、max_completion_tokens、parallel_tool_calls=false。不同兼容服务可能不支持这些字段，必须先做实际协议验证。流有界收齐、校验完整后再执行工具；不把供应商原始流直接送给网页。

失败或未知 usage 保留保守预算。`audit_logs` 中 `action=model.attempt` 记录画像/策略摘要、请求序号、预留和已知用量；不记录 Prompt 或凭据。不支持精确 tokenizer，字节估算可能使小窗口更早拒绝。价格仅代表可信配置，不能视为供应商账单。

## 显式本地 Replay

在 Python 3.11 虚拟环境中执行，依赖沿用现有 Windows lock 文件。以下命令使用示例中的虚构输入、工具和模型响应，不连接商业模型或真实工具。

```powershell
$replayKeyBytes = New-Object byte[] 32
[System.Security.Cryptography.RandomNumberGenerator]::Fill($replayKeyBytes)
$env:LOCAL_REPLAY_KEY = [Convert]::ToBase64String($replayKeyBytes)
python scripts/replay.py record --case examples/v3_1/p3/replay-case.json --out work/replay-baseline.zip --key-env LOCAL_REPLAY_KEY
python scripts/replay.py inspect --bundle work/replay-baseline.zip --key-env LOCAL_REPLAY_KEY
python scripts/replay.py run --bundle work/replay-baseline.zip --key-env LOCAL_REPLAY_KEY
python scripts/replay.py compare --baseline work/replay-baseline.zip --candidate work/replay-baseline.zip --key-env LOCAL_REPLAY_KEY
```

妥善保管签名密钥；上述临时环境变量在新终端不会自动保留。生产操作者密钥与 Replay 密钥应分开。HMAC 只是本地信任，不是 PKI 或多人独立签名。已有输出文件拒绝覆盖，重录使用新文件名。

夹具固定声明消息、权限/对象、只读工具 Schema、精确参数对应的录制输出、模型响应序列和时点。仅支持有界基础 Schema，不支持正则、外部引用、脚本、真实适配器地址或写工具。Runtime/Gateway/Store 使用原实现，模型与工具传输强制录制响应，无命中拒绝，无真实网络回退。同步 Replay 模块仅供独立 CLI，不能嵌入运行中的 API 进程，因为它为可重复测试固定进程时间。

record 先脱敏再运行：显式 `redactions` 值、常见凭据模式和邮件地址会替换；脱敏字段冲突拒绝。自动规则不能识别所有个人信息或任意业务秘密，输入应优先使用虚构数据；操作者必须检查导出内容。`fixture_policy=redacted` 是声明，不能替代人工检查。脱敏改变语义时应修正夹具，不能声称复现了原始私密任务。

ZIP 只有 case.json、recording.json、manifest.json；最大 4 MiB，不向磁盘解压。签名、版本和各文件摘要先验证，run 还要求执行代码摘要一致。升级代码后重新录制候选包，再 compare；不能用旧包加载历史 Python 代码。结构结果参与确定性比较，延迟只单独报告。

compare 固定输入与工具夹具，不同输入为 INCOMPARABLE；权限扩大、证据丢失、Schema 破坏或终态退化为 FAIL。文本差异单列，不自动断言模型质量改善；未知成本不计算精确收益。PASS 仅表示这些本地门禁没有发现退化，不代表任意业务语义或安全绝对正确。候选模型以显式录制响应表示，真实商业模型 comparative 验证仍为 NOT_RUN。

## 验证和回退

`python -m pytest -q --tb=short tests/test_model_router.py tests/test_provider_router_integration.py tests/test_provider_http.py tests/test_replay.py`

Windows 沙箱可能阻止事件循环与回环 socket；应在本机允许权限的 Python 3.11 环境运行。HTTP 测试创建本地临时端口并在结束时关闭；Replay 使用 MockTransport，二者验收单列。移除 provider_policy 可回到旧模式，无数据库迁移。完整覆盖与限制见 `p3-acceptance.md`。
