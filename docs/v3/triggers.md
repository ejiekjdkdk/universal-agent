# 可信事件与本地定时触发

本地 Trigger Gateway 将签名事件或到期定时器转换为已有 `durable_plan` Task。计划验证、快照、步骤租约、审批、恢复和结果查询继续使用既有执行器。没有第二套任务状态机；未连接生产事件系统，也没有外部订阅或任意 URL 回调。

## 本机验收

在项目根目录执行 `python scripts/local_triggers.py --verify`。脚本使用临时目录、随机令牌和回环端口启动 API，发送签名事件、验证重复事件返回同一任务、拒绝篡改字节，并等待事件和定时任务都执行完成。退出时停止服务并清理临时数据，不显示凭据。

`python -m pytest -q tests/test_triggers.py` 覆盖真实 SQLite 的两个连接竞争、持久化重启、事务中途失败、实时撤权、过期事件、重复 ID 冲突及定时器游标回滚。完整 Worker 强制退出和租约恢复仍由 `tests/test_worker_recovery.py` 验证。

## 可信配置

默认不设置 `AGENT_TRIGGERS_FILE`，入口返回 `TRIGGERS_DISABLED`，后台定时器不运行。设置后指向可信本地 JSON 文件，参照 `examples/triggers/triggers.json`；角色文件参照 `examples/triggers/principals.json`。`token_env` 指向已有角色令牌的环境变量，由服务端当前授权表解析身份；`signing_key_env` 指向至少 32 字符的独立 HMAC 密钥环境变量。配置只保存变量名，不保存秘密。

每个来源明确列出可触发的业务以及 `object_types`、`object_ids`。事件范围还必须通过该角色的实时授权，两层约束取交集。空数组表示不允许声明该类型或对象，不表示通配。客户端不能提交租户、组织、用户、能力 URL、计划、预算、会话或模型参数。任务固定使用离线规划和注册的持久计划。若业务有技能绑定，选择唯一可用技能并要求其覆盖计划全部能力；多技能歧义会拒绝接入。接入前还检查技能声明的输入 Schema 与调用预算：计划步骤超预算或输入结构不符时，任务、事件回执和 nonce 均不落库；有效计划的调用预算取服务端计划与技能上限的较小值。

配置每次接入、定时检查和触发任务授权时重新读取；删除或禁用来源、撤销角色、缩小范围以及配置损坏均阻止后续执行。已经结束的任务也受该来源约束，撤销来源后旧任务不会继续向该来源角色开放。角色权限和发布紧急禁用继续由既有 Gateway 实时检查。

## 签名事件协议

`POST /internal/agent/triggers/events` 接受不超过 32768 字节的 UTF-8 JSON，且禁止重复键。请求必须恰好包含下列字段：

```json
{
  "event_id": "build-123",
  "business_id": "project_brief",
  "message": "生成本周简报",
  "context": {"object_ids": []},
  "issued_at": 1793664000,
  "expires_at": 1793664120,
  "execution_mode": "durable_plan"
}
```

示例时间为协议样本，发送前必须替换成当前 Unix 秒。有效期最多 300 秒，未来签发偏差最多 30 秒；已过期事件返回 410，即使该 ID 以前成功过。签名请求头如下：

| 请求头 | 含义 |
| --- | --- |
| `X-Trigger-Source` | 可信配置中的来源 ID |
| `X-Trigger-Timestamp` | 当前 Unix 整数秒，允许前后 300 秒 |
| `X-Trigger-Nonce` | 1–128 个字母、数字、点、短横线或下划线 |
| `X-Trigger-Signature` | 小写十六进制 HMAC-SHA256 |

签名输入为 UTF-8 编码的 `v1\n{source}\n{timestamp}\n{nonce}\n` 加上**原始请求字节**，密钥为 `signing_key_env` 读取的值。`app.triggers.sign` 提供同一实现。不能在签名之后重新序列化 JSON。

首次接入返回 202 和 `{source_id,event_id,task_id,created:true}`；原字节重投返回 200、同一 `task_id` 及 `created:false`。同来源的同一事件 ID 携带不同字节返回 409 `TRIGGER_EVENT_CONFLICT`，包括仅修改 JSON 空白。复用已登记随机数承载不同签名或事件返回 409 `TRIGGER_REPLAY`。来源 ID 是去重边界；来源和事件 ID 记录不会自动过期或删除，以免旧事件再次创建任务。

响应 `Location` 指向常规任务查询接口。签名密钥只能调用触发入口；读取任务仍需要对应业务角色令牌。

## 持久化与限流

`BEGIN IMMEDIATE` 内完成去重、并发及频率检查、当前配置和角色校验、发布选择复核、任务/计划创建、事件回执及随机数登记。内部 `Store.start` 使用同连接嵌套 savepoint，外层回执写入失败会回滚已创建任务。两个进程提交同一事件时只有一个能创建 Task。应用崩溃后重投得到同一持久任务或从未提交的全新接入，不会留下“已有事件但没有任务”的窗口。

每来源默认最多 4 个未完成任务、每 60 秒最多 30 个新任务，分别由 `max_active` 和 `max_per_minute` 配置（1–100）。计数查询持久库，跨连接、进程和重启生效；事件重投不占新任务配额。失败接入保留拒绝审计，不创建任务。凭据、签名和完整入口原文不进入审计。

## 本地定时器

在 `schedules` 中添加可信定义：

```json
{
  "schedule_id": "weekly-brief-v1",
  "source_id": "local-demo",
  "business_id": "project_brief",
  "message": "生成本周简报",
  "context": {},
  "interval_seconds": 3600,
  "start_at": 1793664000
}
```

`start_at` 为整数 Unix 秒；间隔允许 1–86400 秒。API 生命周期每秒检查一次，退出时取消检查循环；与业务 Worker 部署方式无关。多 API 实例共享 SQLite 时以写事务竞争游标，不依靠进程内锁宣称唯一性。

到期事件 ID 固定为 `schedule.{schedule_id}.{due}`。事件回执、任务和 `next_due` 在同一事务提交；任一写入失败全部回滚。重启时错过的多个周期合并为最近一个到期时点，超过该时点 300 秒仍未处理则跳过并审计，避免无限补发。限流或权限拒绝时不推进游标，下次检查重新验证；永久失败需修正配置或删除定时器。

已经持久化的定时定义不可原地改写：更新必须使用新的 `schedule_id`。删除定时定义立即停止后续调度，但不主动取消已经创建的任务；禁用来源则同时阻止触发任务继续执行。这里没有 cron、分布式消息队列、生产 webhook 注册或外部消息确认机制。
