# P0 测试清单与运行记录

基线 ac448df；2026-11-03 在独立工作区执行 `python -m pytest -q --tb=short`：387 passed、12 subtests passed，181.04秒。使用既有 Python3.11 虚拟环境，`python -m pip check`：No broken requirements found。没有通过安装或升级依赖改变基线。

| 分类 | 实际测试 | 解释 |
| --- | --- | --- |
| Mock模型 | test_providers、test_model_flow、test_harness_skills | 模拟模型输出经过实际网关；不是真实模型质量 |
| 接口/安全 | test_api、test_security、test_skill_packages、test_skill_governance | 临时SQLite/TestClient/真实权限检查 |
| 持久与恢复 | test_plan_store、test_worker_recovery、test_approval_recovery | 实际进程、租约、模拟账本；无真实写入 |
| MCP协议 | test_mcp_gateway | 安装官方SDK与本机真实HTTP测试Server；非生产服务 |
| 项目接入 | test_v3_demos、test_external_template | 外部目录部署、实际子进程与回环HTTP |
| 基线快照 | test_v31_compatibility | 新增后1 passed，2.64秒，38条OpenAPI路径/DDL/参数/终态映射 |
| 真实模型/企业联调 | NOT_RUN | 本轮不需要真实凭据和业务数据 |

V31-001～006 的现有行为由上述回归和代码映射覆盖；完整逐用例状态在 P1 acceptance.md 记录。不能把原用例数直接当成48项全部通过。
