# V3 P0 环境审计

审计起点是 V2 修复提交 `88e12bc`。以下结论针对 Windows 本地运行。仓库内置场景、旧客户端和已保存的会话均属于兼容范围。

| 项目 | 实测环境与复查入口 |
| --- | --- |
| Python / 数据库 | CPython 3.11.2 x64，标准库 SQLite 3.39.4。运行 `python -c "import sys,sqlite3;print(sys.version,sqlite3.sqlite_version)"` 可复查。 |
| 服务 | FastAPI 0.136.1、Pydantic 2.13.3、Uvicorn 0.46.0、HTTPX 0.28.1。进程通过 `python run.py` 监听回环地址。 |
| 工具与模型 | Catalog 从受信任的场景目录加载 Python 或固定 HTTP 适配器；模型端点来自本机配置。旧离线模式调用场景指定工具。模型循环经 Gateway 校验权限、参数、结果、轮数和预算。 |
| 存储和恢复 | Store 持久化会话、请求幂等键、调用、审计和事件。计划执行器负责步骤租约、恢复和审批。reactive 和 durable_plan 已经是不同执行契约。 |
| 页面 | 原工作台用相同业务 ID 提交任务、展示证据和查询 Trace。页面通过本地认证接口或手工令牌识别用户。 |
| 依赖复现 | `requirements-win-py311.lock` 固定此 Windows/Python 组合所用的运行与测试依赖。新虚拟环境中运行 `python -m pip install -r requirements-win-py311.lock`。 |

审计先检查 `app/main.py` 的请求契约，再沿 `app/runtime.py`、`app/providers.py`、`app/catalog.py` 跟踪调用，最后核对 `app/store.py`、`app/snapshots.py` 和 Worker。基线与本次运行记录见 [验收报告](v3/acceptance.md)。

## ADR 001 · 扩展现有执行器

模型只负责建议下一步，现有 Gateway 才有权执行工具并留下证据。如果引入一个同样负责工具调用、重试和完成状态的 SDK，将产生两套幂等、撤权和终态判断。本轮选用已有 Python 循环和固定的服务端检查点，新增确定性 Mock 来验证同一调用路径。既有离线模式、真实模型配置入口及持久计划继续使用各自的契约。

## ADR 002 · 分开保存技能和执行授权

标准技能可以只带 `SKILL.md`。导入时把内容保存为无授权草稿；本机操作者在发布时决定业务域和允许的能力。运行时取发布版本与当前用户权限的交集。发布后修改原目录不应改变当前任务，撤销权限则应阻止后续调用。散列用于识别内容，操作者的本地发布动作是本阶段的信任来源。安装带 Python 适配器的业务项目仍须由操作者审查代码。

## 后续依赖与风险

使用 `python -m pip install --dry-run --ignore-installed -c requirements-win-py311.lock -r requirements.txt mcp` 探测在现有锁上添加官方 SDK。解析器尝试了包含 2.3.0 在内的候选版本并返回 `ResolutionImpossible`，其中一个明确冲突是本机锁定的 AnyIO 4.0.0。P2 需单独评估更新依赖或让 MCP 网关使用独立环境，并对 REST/SSE 做回归。

- P2 用明确的 SDK 版本和协议版本验证注册服务器、能力协商、超时、错误与输出校验。P1 集成测试使用本地适配器。
- P3 如改用 PostgreSQL，需要迁移、并发和故障恢复验证。SQLite 的测试结论只适用于已测环境。
- P4 扩展技能发布审批、签名、灰度和评测。P5 专业 Agent 路由另立范围。
