# 运行两个独立的 V3 演示项目

Demo A 查询虚构展品的登记记录。Demo B 从授权的手册片段生成摘录式摘要。两者使用确定性 Mock 模型、进程内只读适配器和本地 SQLite。业务数据均为虚构素材，运行时不需要模型凭据或外部服务。

## Windows PowerShell

从 P1 升级且沿用已有状态目录时，先备份并完成[旧技能显式迁移](../../docs/v3/skillops.md#p1-升级)。全新状态目录直接执行下列步骤。

在底座仓库根目录使用 Python 3.11 安装依赖。依赖已安装的环境可直接跳到运行命令。

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-win-py311.lock
```

一条命令可在同一个工作台启动 A 和 B。

```powershell
.\examples\v3\start-demo.ps1 -Python .\.venv\Scripts\python.exe
```

打开终端打印的工作台地址（默认 `http://127.0.0.1:8765/`），将本次生成的访问令牌粘贴进“本地访问令牌”。如已设置环境变量 `AGENT_ACCESS_TOKEN`，使用已有值。先选“虚构展品登记簿”，点“使用示例”，用 `R-1001` 运行。再切换为“虚构观察手册”，填入 `guide-01,guide-02` 后发送示例问题。展开任务详情可核对实际执行的工具与证据。

也可在第二个 PowerShell 终端执行查询，将服务使用的令牌填入 `$env:AGENT_ACCESS_TOKEN`。HTTP 文件提供同一组请求，支持 VS Code REST Client。

```powershell
$api = "http://127.0.0.1:8765/api/v1/agent"
$auth = @{Authorization = "Bearer $env:AGENT_ACCESS_TOKEN"}
$query = '{"business_id":"demo_records","skill_id":"record-lookup","message":"查询 R-1001","mode":"mock","context":{"object_ids":["R-1001"]}}'
$task = Invoke-RestMethod "$api/chat" -Method Post -Headers $auth -ContentType "application/json; charset=utf-8" -Body ([Text.Encoding]::UTF8.GetBytes($query))
$task.answer
$task.evidence
Invoke-RestMethod "$api/tasks/$($task.task_id)/trace" -Headers $auth | ConvertTo-Json -Depth 8
```

A 的角色可读 R-1001。可核对纸舟模型、东厅 2 号柜、来源 `demo://records/R-1001` 与登记时间。模拟登记簿还包含 R-1002，尝试将该编号加入上下文会被网关拒绝。参见 [A 的 HTTP 请求](demo_a/requests.http)。

在同一请求终端复用上面的 `$api` 和 `$auth`，查询 B。

```powershell
$query = '{"business_id":"demo_fragments","skill_id":"fragment-summary","message":"归纳晨间观察的准备和记录要求。","mode":"mock","context":{"object_ids":["guide-01","guide-02"]}}'
$task = Invoke-RestMethod "$api/chat" -Method Post -Headers $auth -ContentType "application/json; charset=utf-8" -Body ([Text.Encoding]::UTF8.GetBytes($query))
$task.answer
$task.evidence
```

B 的摘要应引用两条片段，分别说明出发前的装备核对与现场记录要求。将上下文缩小到 `guide-01` 可复核仅引用准备要求。参见 [B 的 HTTP 请求与事件重放](demo_b/requests.http)。

两个目录各自包含场景、适配器、数据、角色配置及标准 SKILL.md。启动脚本将选定项目装入临时场景目录，并合并同一虚构用户在两个业务中的权限。它通过本机 CLI 导入包，再显式绑定各自的工具。默认组合工作台将注册表与历史保存在 `var/v3/both`，相同包的重复导入与发布可安全重试。

如需验证单个独立项目，在同一命令追加 `-Demo demo_a` 或 `-Demo demo_b`。各自的默认状态目录分别为 `var/v3/demo_a` 与 `var/v3/demo_b`。也可将项目目录复制到另一处，传 `-Projects <项目目录的父目录>`。使用 `-AgentRoot` 指向底座，`-State` 指定状态目录，`-Port` 选择端口。用 Ctrl+C 停止服务。
