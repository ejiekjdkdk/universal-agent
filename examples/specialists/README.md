# 本地专业 Agent 示例

资料检索 Agent 委派给已注册的资料查询工具。文本分析 Agent 委派给字符统计工具。两者使用确定性的本地处理器，子调用使用父任务相同的身份、对象集合、数据库、取消标记与调用预算。

在仓库目录运行。

```powershell
$env:AGENT_SCENARIOS=(Resolve-Path examples/specialists/scenarios).Path
$env:AGENT_SPECIALISTS="1"
$env:AGENT_DATABASE=(Join-Path (Get-Location) "var/specialists/agent.db")
python run.py
```

打开回环地址工作台，选任一场景，点「使用示例」后运行。Trace 展示委派、叶子工具调用和返回。资料检索返回有版本的来源，统计结果有无外部来源的局限说明。关闭服务后将 AGENT_SPECIALISTS 设回 0 可以禁用委派。

声明式 `agent` 适配器的 `target` 只能指向同场景内的只读叶子能力。包装能力和叶子能力均须被当前用户及所选技能授权，输入输出 Schema 与对象字段必须相同。注册拒绝递归委派。交互任务允许此演示，持久任务应把专业能力展开为显式计划步骤，使每个步骤具有独立租约和恢复记录。
