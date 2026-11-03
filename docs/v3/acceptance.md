# V3 P0＋P1 本机验收

环境、依赖与执行器选择见 [P0 审计](../p0-environment-audit.md)。[双项目启动与 HTTP 示例](../../examples/v3/README.md)使用和测试一致的两个项目包。项目 A 查询虚构展品，项目 B 只汇总当前身份可读的手册片段。

| 设计用例 | 验收路径 |
| --- | --- |
| T01–T06 | `tests/test_skill_packages.py` 与 `tests/test_harness_skills.py`。从只有 SKILL.md 的目录导入，检查草稿不可发现、发布依赖、冻结内容、禁止越界及脚本、旧版本运行中的撤销。读取一份引用后模型才能获得该引用的正文。 |
| T07、T25、T34、T35 | `tests/test_v3_demos.py` 在仓库之外安装 A，部署 B 后重启；用同一公开接口查询旧任务、新任务、Trace 和带 Last-Event-ID 的事件流。校验跨技能和对象越权、来源编号、调用 ID 及唯一终态。原有 API 测试继续覆盖不带技能的新旧客户端。 |
| T13–T16 | 本地适配器及 HTTP 模拟上游。覆盖输入/输出 Schema 错误、超时、注入工具描述试图调用未授权能力。见 `test_catalog.py`、`test_security.py`、`test_tool_concurrency.py`、`test_harness_skills.py`。 |
| T17–T22 | 可信身份与跨组织资源、任务中途撤权、预算耗尽、损坏的结构化响应和未执行工具却声称成功。见 `test_api.py`、`test_security.py`、`test_providers.py`、`test_harness_skills.py`。 |
| T30–T33 | 只访问配置的适配器地址、拒绝重定向及真实写入；返回凭据时中止并验证事件重放不含明文；检索为空时返回局限说明。见上述 Catalog、Security、Harness 和审批测试。 |

**浏览器实测**使用本机 Uvicorn、无头 Chrome、随机令牌、临时 SQLite 和确定性模型，通过默认双项目启动脚本运行。在同一页面点选场景、使用示例、执行并展开历史；记录查询任务 `task_d9b52fdc15f64fabbe5fa4499a1dbb0a` 有 1 条证据，文档摘要任务 `task_2d5c2dbaa0224f10a9a2a02c07d2cd2f` 有 2 条证据，两者均显示所选技能。临时运行结束后数据库释放；按指南重跑可生成新的任务 ID。

## 运行命令与结果

- 基线 `88e12bc` 的独立源码副本运行 `python -m pytest -q`。253 项通过，另有 12 个子测试通过。
- 新版运行 `python -m pytest -q --tb=short`。277 项通过，另有 12 个子测试通过，最终复测用时 100.21 秒。此套件包含临时文件、Windows 本地套接字、模拟模型和 HTTP 测试替身。
- `python -m compileall -q app scripts examples/v3` 和 `node --check static/app.js` 校验 Python/JavaScript 语法。
- 类型检查采用 mypy 1.18.2、types-PyYAML 6.0.12.20261030，并使用 `--ignore-missing-imports --check-untyped-defs`。基线 app 为 131 条诊断，本次 app 和新增 CLI 合计 129 条，均在原有模块。逐文件比较诊断种类与数量，没有新增类别或增加项。全库类型检查仍返回非零。单独对新增技能注册表和 CLI 使用相同选项并加 --follow-imports=silent，2 个文件通过。

## 适用边界与回滚

P1 是本机受信任操作者的发布流程。任务把工具调用与实际返回的证据关联，引用标签需由本次调用产生；语义上是否充分支持每句话仍需要人工判断。敏感输出保护覆盖当前已知凭据和常见令牌字段。模型循环的调用预算与适配器物理重试分别设限。技能包内附带的脚本不进入执行路径。真实模型推理、MCP 互操作、生产数据源、签名审批以及 PostgreSQL 并发部署各自需要独立验收。

如要回到 V2，先停止服务，用 V2 提交 `88e12bc` 启动独立目录，并指向升级前备份的数据库及场景。双项目启动脚本默认使用 `var/v3/both`，与原工作台分开。保留 V3 数据目录可再次查看新任务。可用 `git diff 88e12bc...HEAD` 核对源码修改。
