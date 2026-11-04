# P4 本机发布检查与回退

本流程只核验操作者信任的本机代码与虚构夹具，不执行生产发布。`environment` 的 PASS 仅证明环境；完整测试及检查通过才能输出 `LOCAL_RELEASE_READY`，生产始终为 `NOT_RUN`。

## 固定运行环境

本轮使用独立 CPython 3.11.18 / Windows x86_64 / SQLite 3.53.1，原 Python 3.11.2 / SQLite 3.39.4 环境保留。原 `requirements-win-py311.lock` 不变，不在原环境原地升级。

解释器来源：Astral python-build-standalone 的 20261026 固定发行版，文件 `cpython-3.11.18+20261026-x86_64-pc-windows-msvc-install_only_stripped.tar.gz`，SHA256 `67215a7bf1643e33d01b17ee3f1e91a8b0b39163746855ee02594bf0608f68b4`。检查实际加载的 SQLite，不从 Python 版本推断。门禁记录 source id、线程模式、扩展与 DLL 摘要，要求原生证明完整且版本含官方 WAL 修复。

SQLite 官方说明受影响范围与修复见 <https://sqlite.org/wal.html#walreset>；3.51.3+、3.44.6/3.50.7 所在回补分支可用。旧环境可用于历史调查，不作为 P4 的并发 WAL 发布环境。此门禁的已验证目标限 Windows 64 位 Python 3.11。

## 执行检查

先在干净的候选 Git 工作树、固定依赖环境中运行；源码目录不能有 `.env`。可信配置与密钥留在源码外。证据目录必须全新、位于源码外，已有目录拒绝覆盖。

```powershell
python scripts/release_check.py environment
python scripts/release_check.py verify --output C:/agent-evidence/p4-run-001
```

第二条执行完整 pytest，而后执行 pip check、compileall、两份网页 JS 语法检查、限定模块 mypy 与 git diff 检查。报告绑定开始及结束的 commit/tree；中途源码变化拒绝就绪。必需用例缺失、失败、跳过、未完整执行或辅助检查失败均阻断。报告、pytest 原始日志、JUnit 和辅助日志一并保留；文件摘要供本机核对，不是外部签名或生产认证。

完整测试范围明确从仓库根 `.` 发现，包含 `tests` 及 `scenarios/text/test_text.py`；使用证据目录中的固定空 pytest 配置及仓库 confcutdir，不继承上级目录的 addopts/testpaths/ignore/conftest。两项既有场景测试另列必需节点，避免目录假设遗漏。收集阶段的模块跳过/失败与取消选择也会阻断；报告分别记录这些数量，不能把未收集模块内未知数量的用例冒充已执行节点。

Windows AppContainer 可加 `--sandbox-temp`。Python 3.11.10+ 的 `mkdir(0700)` Windows ACL 行为会导致沙箱硬链接测试目录失去继承权限；该选项仅在沙箱自己的 Temp 创建短路径并保留继承 ACL，不修改产品目录或测试断言。普通本机运行无需此选项。测试执行时移除宿主 `AGENT_`/`PYTEST_` 环境变量，夹具显式设置虚构配置；此 CLI 仍会执行受信仓库程序，不能承载不可信测试，也不可嵌入在线 API。

## 配置与渐进启用

默认保持旧路径：`AGENT_CONTEXT_COMPILER=0`、不配置 `AGENT_P2_POLICY`、不配置 `AGENT_PROVIDER_POLICY`、`AGENT_SPECIALISTS=0`、模拟/真实写开关均为 0。显式启用前分别完成 P1/P2/P3 文档中的本机检查。外部 Worker 模式先按现有文档配置同一权威数据库与租约，再启动进程；本轮不证明跨主机或 PostgreSQL。

灰度按原固定主体分组，保留候选评价、激活和回滚审计。冻结/吊销与实时权限收缩继续生效。不要为恢复历史任务重新启用已撤销权限，或把结果过期/删除解释为允许重新执行工具。

## 保留当前状态的代码回退

1. 暂停接单和触发来源；停 API 内调度与独立 Worker，确认无活动写入/租约持有者，保留未决审批和未知结果记录。不要同时运行不同代码版本写活动库。
2. 记录当前任务库、Skill 库、快照/发布注册表、可信配置、`.operator-key` 及结果密钥版本。使用 SQLite backup API 获取一致副本；不要只复制活跃 `.db` 丢弃 WAL。备份与密钥留在受控位置，禁止入库或源码包。
3. 切换到已验证的代码基线 `ed4f85b`，继续使用含修复 SQLite 的隔离环境与**当前权威状态**。这不是恢复旧数据库。保留 P2 结果读取策略和所有仍被引用的旧结果 key；直接关闭 P2 会使历史引用返回 `RESULT_STORE_DISABLED`，不是通用回退办法。
4. 先核对历史/幂等/事件和 HMAC/权限，再恢复接单和调度。未知写结果只查授权回执，禁止重发；过期或删除结果继续拒绝。
5. 独立的灾难备份恢复只在停写的虚构副本演练：任务库、Skill 库、注册表/快照、密钥和配置作为同一恢复单元核对，不覆盖正在运行的权威库。本机演练不构成生产 RPO/RTO 承诺。

本轮实际验收结果及限制见 `p4-acceptance.md`；接口对应见 `p4-interfaces.md`。
