# V3.1 P4 Incremental Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 在隔离修复 SQLite 的环境完成可复现的本机 P4 发布门禁，保留原接口和运行语义。

**Architecture:** 原执行器、Gateway、Store、SkillOps 和发布机制保持。仅增加原生运行时指纹、独立本机发布验收入口及缺失的端到端测试；输出真实证据和回退说明。

**Tech Stack:** Windows x86_64、CPython 3.11.18、SQLite 3.53.1、原 requirements-win-py311.lock；不增加应用依赖。

**Spec:** `docs/v3_1/p4-design.md`；原始 `docs/design/V3.1.md` §9/10/14、V31-044～048。

## Global Constraints

- 保留原 Python 3.11.2 环境；不原地更换解释器、密钥或数据库。
- 公共 API/Schema/错误/Task/Step/权限/审批/异步/并发/取消/恢复语义保持；partial 仍映射 task_completed。
- 模型和业务数据虚构；写入仅原本地模拟账本；商业模型、企业写入、PostgreSQL/分布式及生产发布 NOT_RUN。
- 不自动录制日常 API/网页；不削弱 Replay 的版本、HMAC、Schema 与强制 MockTransport。
- 不创建第二 Runtime/Store，不增加生产测试 Hook，不做数据库迁移，不合并 main。
- 注释和 Docstring 中文；源码、凭据和测试临时数据分开，证据不可覆盖已有目录。

## Review Focus

1. Python/pip 相同但 SQLite 原生库变化：旧 Replay 必须拒绝，归 Task 1 的 bundle 拒绝测试。
2. 网络中途断开不能影响任务生命周期或引起物理重执行，归 Task 2 的两种执行模式测试。
3. 提交前/后退出与旧租约竞争不能重复写，归 Task 3 的真实双 PID 故障切点测试。
4. 回退不得丢新任务/审计、遗失结果 key 或复活吊销，归 Task 2 的代码/配置切换测试。
5. 缺少用例、测试失败/跳过、缺环境证明不能输出完整就绪，归 Task 4 的证据汇总门禁测试。

## 已完成的环境前置

- [x] 核对 main/P3 远程与干净工作区，创建独立 codex/v31-p4。
- [x] 下载并核验固定发行摘要；运行 Python 3.11.18 / SQLite 3.53.1，确认实际 DLL/source id/摘要及 threadsafety=3。
- [x] 使用原锁安装到独立 venv，pip check 通过；复核原环境四个关键文件无变化。
- [x] 原代码完整基线 597 passed、12 subtests，265.35 秒，无失败/跳过。初始失败和 Windows ACL 定位证据保留在会话 work。

## Task 1: 原生运行时证明和精确 Replay

**Files:** Create `app/runtime_environment.py`, `scripts/release_check.py`, `tests/test_release_environment.py`; Modify `app/replay.py`, `tests/test_p3_audit.py`。

**Interfaces:**
- Produces `sqlite_runtime() -> dict[str, object]`，字段包含 version/source_id/threadsafety/extension_sha256/library_sha256，指纹不包含绝对路径或凭据。
- Produces `sqlite_wal_fixed(version: str) -> bool`，接受 3.51.3+ 或官方 3.44.6+/3.50.7+ 同分支回补；未知格式拒绝。
- Produces `environment_report() -> dict`、CLI `python scripts/release_check.py environment`。环境 PASS 时 release_status 仍为 NOT_RUN。
- Consumes 原 `replay.record_case/run_replay/_engine_digest`，只增摘要输入。

- [x] 写 bundle 测试：记录成功后只改变提供的 SQLite 身份，run_replay 必须在执行前拒绝；旧代码应 DID NOT RAISE。
- [x] 写环境门禁测试：受影响、回补、未知版本；锁不符/缺原生证明不能就绪；CLI 环境模式不得宣称完整验收。
- [x] 在隔离解释器运行上述测试并保存 RED 日志；实现最小运行时采集与 CLI 环境子命令，Replay 加入 SQLite 身份。
- [x] 运行 `tests/test_release_environment.py tests/test_replay.py tests/test_p3_audit.py`，预期全部 PASS；提交并记录命令/结果。

## Task 2: 真实 SSE、版本切换与历史回退

**Files:** Create `tests/p4_helpers.py`, `tests/test_p4_stream_release.py`。

**Interfaces:**
- Produces 测试辅助 `launch(stack, root, env, directory, label, *args)`，返回 `(Popen, log_path)`；沿用已有 `test_worker_recovery.kill/wait_until`。
- 测试辅助只用实际 Python/API/SQLite，本地端口和虚构目录；Windows 用 base executable + 当前 site-packages，确保 kill 命中执行进程，ExitStack 回收所有子进程。
- Consumes 原 `/chat/tasks/events` 与管理员场景发布入口、Releases/SkillRegistry/ResultStore。

- [x] 增加 `test_real_disconnect_replays_only_persisted_events`，参数 reactive/durable_plan；按适配器探针阻塞、消费初始帧后断开，释放后重连；断言 ID/物理计数/唯一终态/空尾游标。
- [x] 增加 `test_code_and_configuration_rollback_preserve_history`：固定虚构主体分组，场景灰度—激活—回滚，当前代码停启后切到 git archive 的 ed4f85b。共享当前数据，验证新旧任务、幂等、事件/审计、HMAC 与结果 key；实时撤权继续拒绝。
- [x] 在完全停写的虚构状态上执行 SQLite backup/恢复副本核验，不覆盖活动库或历史回退数据。
- [x] 运行新测试，针对既有行为允许初始通过；如暴露产品缺陷，保存反例后再最小修复；回归既有 API/发布/版本测试并提交。

## Task 3: 双进程审批模拟写入恢复

**Files:** Create `tests/test_p4_write_recovery.py`; Reuse Task 2 helpers。

**Interfaces:**
- 测试进程使用真实 `app.worker.main`、原 `Store._transaction/process_mock_write`；测试脚本在生产事务外层包装确定性屏障，不改生产类。
- Consumes 原公开审批 API、同一 SQLite 的 mock_ledger/mock_receipts/calls/steps/events。

- [x] `test_two_workers_commit_once` 覆盖 normal/before_commit/after_commit：两个实际 PID 同时运行；提交前/后屏障退出实际写进程，接手恢复后对象 version=1、回执/调用=1、单终态，旧执行身份不伪造成功。
- [x] `test_two_workers_do_not_resend_unknown_commit`：真实事务写入后模拟确认丢失，原 OUTCOME_UNKNOWN 保留，另一进程不重发，授权回执可核对。
- [x] `test_two_workers_respect_cancelled_approval`：审批等待期间取消，不再落账；已有提交的取消不回滚。
- [x] 运行新增测试与 `tests/test_approvals.py tests/test_approval_recovery.py tests/test_plan_store.py tests/test_worker_recovery.py tests/test_high_risk_recovery.py`，预期全部 PASS；提交和更新账本。

## Task 4: 完整门禁、配置与交付

**Files:** Modify `scripts/release_check.py`, `tests/test_release_environment.py`, `.env.example`; Create `docs/v3_1/release-readiness.md`, `p4-acceptance.md`, `p4-interfaces.md`，必要示例配置。

**Interfaces:**
- Produces `verify_release(output: Path, *, sandbox_temp: bool = False) -> dict` 与 CLI `verify --output <全新目录> [--sandbox-temp]`。
- 记录 commit/tree/实际运行时/锁/夹具与测试结果，强制完整 pytest 与 V31-044～048 的必需节点；节点缺失、FAIL/skip/interruption 不输出 LOCAL_RELEASE_READY。production_status 固定 NOT_RUN。
- `--sandbox-temp` 只对 Windows 沙箱私有 Temp 中的测试目录使用短名和继承 ACL，不修改产品权限；测试子进程不继承 AGENT 生产配置。

- [x] 先写门禁失败测试：缺必需节点、跳过/失败/未执行、环境不符、已有证据目录拒绝覆盖；成功报告仅限 local/test。
- [x] 实现环境检查、测试执行及结果汇总；复用固定依赖和标准库，不创建新任务状态或常驻服务。
- [x] 整理默认关闭开关、固定运行环境、停接单/停调度/停 Worker、保留当前数据的代码回退，以及独立备份恢复步骤。
- [x] 补齐 FE/BE 矩阵和 P0～P4 安全证据映射；商业模型/企业/PG/分布式/生产写 NOT_RUN，未确认性能数据不虚构比例。
- [x] 完整 pytest、pip check、compileall、两 JS node --check、限定新增模块 mypy、git diff --check；有失败先复现定位。
- [x] 独立整分支审查，确定阻断问题逐项 RED→GREEN 并回归；发布门禁和文档记录真实结果，保留失败日志。
- [x] 按既有上传授权发布 P4 功能分支并核验远程内容；main 与 P3 分支不移动，不部署生产；产出源码包和验证回执。
