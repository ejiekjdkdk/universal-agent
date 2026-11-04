# P4 前后端与接口兼容矩阵

P4 不新增 HTTP 路由、请求字段、数据库迁移或 SSE 枚举；新增的是独立本机发布 CLI 和原生 SQLite 执行指纹。前端源码无需适配。

| 使用面 | 后端/证据 | P4 行为 |
| --- | --- | --- |
| 业务切换、技能、工具与提问 | 原业务与 `/chat` 契约，`test_api.py` | 保留原字段、权限和错误 |
| 异步任务与取消 | 原 task 路由，`test_durable_api.py` | 保留任务与审批/取消状态 |
| 来源、Trace、历史 | 原任务结果/事件和审计读取 | 保留主体隔离、幂等和历史；读操作不重执行 |
| SSE | 原事件路由与 Last-Event-ID | 重连只读取持久事件；partial 仍映射 `task_completed` |
| 发布、技能冻结/吊销 | 原发布管理与 Skill 治理入口 | 既有授权和审计不变，回退不复活撤权 |
| P2 可选结果读取 | 原受权结果接口，`test_result_store.py` | 保留 key 与策略；缺失/过期/删除继续拒绝 |
| P3 模型路由 | 原 model settings/可信策略 | 无配置时旧路径；无商业模型新增验证 |
| 显式 Replay CLI | 原 record/inspect/run/compare | 指纹增加实际 SQLite 身份；仅原生库变化也拒绝旧精确包 |
| 发布验收 CLI | 新 environment/verify | 无在线接口；环境 PASS 与完整本机就绪分开；不部署 |

`tests/fixtures/v31-baseline.json` 和 `tests/test_v31_compatibility.py` 固定 V3.1 兼容基线；P0～P3 已允许的可选扩展仍由各阶段测试校验，不把基线文件当成全部当前路由清单。完整门禁要求契约快照、API 和 durable API 的实际测试节点通过，再分别核对 SSE、发布回滚和双 Worker。

精确 Replay 的代码/Python/平台/依赖锁/实际安装包校验保持，并加入 SQLite version、source id、线程模式、扩展与库摘要。升级运行时后应以同一固定虚构输入重新录制候选再差分，不加载旧包中的程序。HMAC 仍是本地信任，自动脱敏不能识别任意秘密。
