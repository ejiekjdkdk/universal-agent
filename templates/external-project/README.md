# 给独立项目接入 Universal Agent

这个模板演示一个已有项目如何调用底座，并把自己的 API 注册成工具。它有两个服务进程。项目后端接收问题，底座负责运行任务。需要业务数据时，底座通过项目提供的只读 API 查询记录。项目后端再按任务 ID 从公开 API 取回 Trace，连同回答和证据一起返回给调用方。

```text
客户端 → 项目 POST /ask → 底座 POST /api/v1/agent/chat
                             └─ lookup_record → 项目 GET /v1/records/DOC-101
         项目后端       → 底座 GET /api/v1/agent/tasks/{task_id}/trace
客户端 ← 回答、来源原文、工具调用、Trace
```

## 复制并运行

从底座仓库根目录打开 PowerShell，使用 Python 3.11 或更新版本。以下命令把模板复制到与底座并列的目录。目录已存在时请另选新名称。

```powershell
$agent = (Resolve-Path .).Path
$project = Join-Path (Split-Path $agent -Parent) 'my-agent-project'
if (Test-Path -LiteralPath $project) { throw "目标目录已存在，请指定一个新的目录" }
Copy-Item -LiteralPath (Join-Path $agent 'templates/external-project') -Destination $project -Recurse
Set-Location -LiteralPath $project
python -m venv .venv
$python = (Resolve-Path .venv/Scripts/python.exe).Path
& $python -m pip install -r (Join-Path $agent 'requirements.txt') -r requirements.txt
& $python -X utf8 demo.py --agent-root $agent
```

在 macOS 或 Linux 上，复制同一目录后，也可在同时安装了两份依赖的 Python 环境中执行 `python demo.py --agent-root /absolute/path/to/universal-agent`。Windows 是本轮实测环境。

脚本要求两个项目目录彼此独立。它生成临时令牌，分配本地端口并启动两个服务，以离线模式查询 `DOC-101`。输出 JSON 包含回答、来源 ID、工具和实际 Trace 事件。默认记录的状态为「已发布」。你可以只改本项目 `records.json` 中的状态，再次运行，核对答案与证据是否同步变化。演示结束时关闭自身启动的进程并清理临时数据库。

## 把模板换成自己的项目

| 文件 | 项目负责的内容 |
| --- | --- |
| `project_api.py` | 示例项目后端。已有后端可以复用其中 `/ask` 的两次公开 HTTP 调用。将演示用的项目令牌替换为自己的身份授权逻辑。 |
| `scenarios/project_records/scenario.json` | 注册场景、工具、权限、入参和返回结构。修改场景 ID 时同步修改目录名及 `/ask` 发送的 `business_id`。 |
| `scenarios/project_records/adapter.py` | 把工具参数映射到项目自己的只读接口，把项目返回值映射为 `data` 和 `evidence`。此模板的本机地址与路由由适配器固定，提问者只能提供记录编号。 |
| `records.json` | 本项目独有的示例数据。接入已有业务服务时，可以直接使用该服务的数据。 |
| `demo.py` | 可重复执行的跨进程验收。两次提交相同幂等键，检查项目只成功读取一次记录。它还检查缺失记录、错误令牌、停用上游后任务失败及 Trace 与证据的关联。 |

两个进程通过 HTTP 通信。底座在启动时从磁盘加载受信任的场景包，因此需要能读取该目录。将 `AGENT_SCENARIOS` 指向项目的 `scenarios` 时，该目录会作为本次运行的完整场景目录。内置通用 HTTP 适配器要求固定 HTTPS 目标；本模板通过已支持的本地 Python 适配器接入回环开发服务。

## 长期运行并接入自己的前端

按下表为两个进程注入环境变量。底座使用自己的 `python run.py` 启动，项目服务使用本目录的 `python project_api.py` 启动。两个命令可运行在各自的虚拟环境中。

| 变量 | 由哪个进程读取 | 示例或含义 |
| --- | --- | --- |
| `AGENT_SCENARIOS` | 底座 | 独立项目 `scenarios` 的绝对路径 |
| `AGENT_DATABASE` | 底座 | 此项目专用的 SQLite 文件绝对路径 |
| `AGENT_PORT` | 两者 | 默认 8765，底座监听端口 |
| `PROJECT_API_PORT` | 两者 | 默认 8877，项目 API 监听端口 |
| `AGENT_ACCESS_TOKEN` | 两者 | 项目后端调用底座的高熵令牌 |
| `PROJECT_API_TOKEN` | 两者 | 演示项目独立的高熵令牌，适配器请求项目记录接口时使用 |

令牌在本地配置，两个值必须不同。模板的 `/ask` 和记录 API 按单用户服务设计，真实多用户项目应在自己的后端落实用户及资源权限。

项目后端运行后，可在已设置 `PROJECT_API_TOKEN` 的 PowerShell 中调用自己的接口。

```powershell
$headers = @{ Authorization = "Bearer $env:PROJECT_API_TOKEN"; 'Idempotency-Key' = [guid]::NewGuid().ToString() }
$body = @{ message = "请查询 DOC-101 的状态"; mode = "offline" } | ConvertTo-Json
Invoke-RestMethod http://127.0.0.1:8877/ask -Method Post -Headers $headers -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($body))
```

请求方应检查返回的任务 `status` 与 `error`。重试同一次提交时沿用原幂等键。使用底座已有的模型模式时，在底座端配置支持工具调用的模型，将请求的 `mode` 设为 `openai`。本模板的自动验收使用离线模式。
