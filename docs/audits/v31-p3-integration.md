# V3.1 P3 合并与回归记录

日期：2026-11-04。用户明确授权将 P3 合入 main，合并前核对工作区与最新远程状态，回归通过后推送 GitHub，并保留 P3 功能分支。

## 合并与预检

- main 从 `82b1a7337ff71b6bd5ffc11ec3daca7144d0d669` 快进到 `f140691c6d5bc50756c6e38da9ef78427c1b5873`，包含 P3 的 5 个提交；21 文件、2172 行增加、8 行删除。无冲突、未压缩或改写历史。
- 合并前 fetch origin 成功，本地与远程 main/P3 均一致。main 的 Git 状态干净。
- 沙箱无法切换到 P3 工作目录；改用绝对路径逐个核对 224 个受控文件与 Git blob、暂存区及额外文件。按 core.autocrlf 归一换行后无内容差异、无缺失、无暂存变更，额外文件仅为已忽略缓存。未修改持久 Git 配置。
- 最终 P3 提交补做独立只读子 Agent 审查，范围为 `82b1a73..f140691`，没有已确认 Critical/Important/Minor。审查未替代合并后回归，也不代表绝对无漏洞。
- 本地及远程 `codex/v31-p3` 保持 `f140691`，原工作树与其他功能分支继续保留。

## main 验证

在合并后的 main 上执行原有完整测试集：**597 passed、12 subtests passed，309.02 秒，退出码 0，无失败或跳过**。覆盖既有 538 项与 P3 新增 59 项。

依赖检查、两新增模块限定 mypy、两个静态 JS 的 node --check、compileall 与 Git diff 检查已通过；限定 mypy 不代表全库零错误。执行回归时 main 的代码树与 P3 最终提交完全一致。集成记录只补充文档。

## Windows 沙箱测试环境

默认临时路径触发 WinError 206；工作区短路径又受到沙箱硬链接创建限制。最终使用沙箱自带临时目录，并由独立 pytest 验证脚本将 tmp_path 子目录名缩短为 `t<n>`。该脚本保留 Windows 子进程入口保护，只调整临时目录命名；原有全部测试和断言保留，仓库生产代码及测试文件未修改。7 项相关针对性测试先通过，27.43 秒。

本机验证使用 `PYTHONUTF8=1`、`PYTHONIOENCODING=utf-8`，执行 `python -u run_p3_regression.py`；该脚本调用原有 pytest 完整收集，参数为 `-q -ra --tb=short --basetemp <沙箱 TEMP>/p3`。辅助脚本与完整成功日志分别为：

- `C:/Users/Lenovo/Documents/Codex/2026-11-04/universal-agent-git-2026-11-04/work/run_p3_regression.py`
- `C:/Users/Lenovo/Documents/Codex/2026-11-04/universal-agent-git-2026-11-04/work/p3-main-compact-fixture-full.log`

同目录保留前序失败及专项复验日志；没有以跳过硬链接或删除失败断言获得通过。

## 发布与范围

这是推送前的集成与验收记录；推送后需读取远程 main/P3 头并核对 Git 祖先关系，实际结果在交付回执记录。采用普通快进保护，不强制推送。分支交付记录中的“未合并 main”描述其历史状态。

P3 的既定边界继续有效：Replay 只覆盖显式本地虚构/脱敏测试，真实 HTTP 仅为本机模拟模型；商业模型、真实企业写入、PostgreSQL/分布式生产运行均未验收。详见 [P3 验收](../v3_1/p3-acceptance.md)与[审计记录](v31-p3-review.md)。
