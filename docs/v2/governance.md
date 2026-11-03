# V2 本地版 · 审批、发布与离线验收

从仓库根目录运行以下命令。使用 Python 3.11 或更新版本，并安装 `requirements.txt`。验收和演示都使用临时目录、随机凭据、回环 HTTP 和本地 SQLite。退出后演示数据会被清理。

```sh
python scripts/local_governance.py --verify
python scripts/local_governance.py
```

第一条自动走完批准、重复提交、发布和回滚，并输出版本和回执。第二条启动交互服务。在终端显示的工作台地址输入**申请人令牌**，选择「草案变更」，提交一条变更。工作台进入「等待审批」后，打开独立的 **/review** 页面并输入**审批人令牌**。核对对象、变化的字段、版本和提案摘要后批准或拒绝。在已批准队列可以查看提交状态和回执，工作台可回看完整任务轨迹。

## P2 · 审批与本地模拟账本

在持久计划里，只有显式启用 `AGENT_MOCK_WRITES=1` 且适配器类型为 `mock_ledger` 的模拟提交步骤可以申请审批。申请人与审批人必须是同租户、同组织的两个独立用户。双方都要有目标对象权限，审批人还需具备该场景的 `approval.decide`。正式写入保持关闭，启动配置要求 `AGENT_REAL_WRITES=0`。

提案绑定对象、申请人、参数摘要、计划和版本快照。可选的 `expected_version` 用于明确对象前置版本；省略时服务端在创建提案的事务内读取当前版本。审批后 Worker 重新核对授权、发布禁用状态和对象版本，再在同一笔本地事务中记录账本变化、唯一回执和执行结果。重复批准使用同一幂等键。撤权、过期、变更或竞争更新会阻止旧批准继续执行。发生确认超时时，任务记录 `OUTCOME_UNKNOWN`。授权用户按审批 ID 只读查证原回执，查证本身不会重新提交。

| 公开接口 | 用途 |
| --- | --- |
| GET /api/v1/agent/approvals | 按授权、状态、limit 与 offset 读取审批队列 |
| GET /api/v1/agent/approvals/{id} | 提案、字段变化、摘要、决定资格及提交状态 |
| POST /api/v1/agent/approvals/{id}/decision | 提交 `decision`、`proposal_digest` 及可选 `comment`，请求头需带 Idempotency-Key |
| GET /api/v1/agent/approvals/{id}/receipt | 核对已记录的模拟回执、提交版本和当前账本版本 |

如需保留数据，沿用 `python run.py` 与独立 Worker 的启动方法。指定 `AGENT_DATABASE`、`AGENT_SCENARIOS`、`AGENT_PRINCIPALS_FILE`，按[角色配置样例](../../examples/governance/principals.json)在服务端注入不同用户令牌。角色文件只存环境变量名与授权范围。角色文件更新后，API 和独立 Worker 在授权时重新读取；建议原子替换文件。令牌环境变量变更需重启相应进程。

## P3 · 发布管理

使用单独的 `AGENT_ADMIN_TOKEN` 访问 `/api/v1/agent-admin`。该令牌与用户和服务令牌必须不同。设置 `AGENT_RELEASE_CANDIDATES` 为已审核的本地候选根目录，例如 `<root>/vnext/notes/scenario.json`。候选目录是运维侧的可信输入，管理 API 接收候选名称与场景 ID。

1. 向 `POST /releases/validate` 或 `POST /releases` 传入 `{"business_id":"notes","candidate":"vnext"}`。后者把通过实际评测的候选登记为 staged。相同场景版本不可绑定不同内容；破坏性变更需要新的主版本。响应中的 `release_id` 标识该内容快照。
2. 向 `POST /releases/{id}/activate` 传入 `{"canary_percent":10}`。服务端按身份和场景稳定分桶；传 100 即全量发布，0 表示零流量。新任务选定版本后持久化绑定，进行中的任务继续使用已选快照。
3. 向 `POST /releases/{id}/rollback` 传入 `{}`，让新任务回到之前的发布。回滚、激活也会重新检查门禁。使用 `GET /releases/{id}` 查看状态、当前绑定和评测结果。
4. 紧急停用一个快照可调用 `POST /releases/{id}/revoke`。停用某场景的工具可调用 `POST /capabilities/{name}/revoke` 并传 `business_id`。数据库中的禁用会在 API 和独立 Worker 的下一次受控调用中生效。已经发出的网络请求不能撤回。普通回滚不会取消安全禁用。

上述路径均以管理前缀为基址。发布回滚和软件降级是两个操作。升级前请停服务并备份 SQLite 数据库及旁边的 `plan-snapshots/`；回退应用程序时应配套还原对应代码、数据库和快照，避免用旧进程处理新状态。

## 评测、指标与 Trace

```sh
python -m app.evaluations --scenarios scenarios
python -m app.evaluations --scenarios examples/durable/scenarios
python -m app.evaluations --scenarios examples/governance/scenarios
```

独立维护的金标套件在 `evals/v1/`。发布门禁会核对受审代码、工具绑定、固定输入的路由、输出、证据与强制安全用例。未知场景须先安装对应的受信任套件。只读 HTTP 能力可使用精确匹配的离线录制响应验证协议。评测结果含套件摘要、版本、指标与失败用例。[编写金标与录制规则](../../evals/README.md)

管理员可以读取 `GET /observability/metrics`、`GET /observability/alerts` 和 `GET /observability/traces/{task_id}`。最后一个接口输出分页的 OTLP JSON，连接请求、步骤、审批与工具执行。数据窗口、样本量与缺失值随指标返回；告警覆盖权限拒绝、审批待办、未知提交结果及账本异常。导出不包含原始输入、文档片段或令牌。用户工作台的回答和原始证据继续受任务权限保护。

模型端点提供有效用量时，指标保留观测到的输入和输出 Token。设置 `AGENT_MODEL_INPUT_USD_PER_MILLION` 和 `AGENT_MODEL_OUTPUT_USD_PER_MILLION` 后，可按每轮收到用量时的单价估算美元成本。缺失用量或价格时以覆盖率和空值区分，离线测试不能推断真实账单。

这是本机离线验收的范围。真实推理质量、企业 SSO、外部业务写入、生产数据库高可用和线上容量，需要各项目独立对接与验证。
