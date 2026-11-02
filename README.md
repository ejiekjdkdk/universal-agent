# Universal Agent

Universal Agent 是一套可跨项目复用的通用 Agent 底座。它负责模型接入、工具发现与调用、权限校验、会话、任务、证据和执行轨迹（Trace）。接入新项目时，将该项目的业务接口或处理逻辑封装成场景包，由项目自己的前端或后端通过统一 API 发起任务。

例如，给工单系统增加助手时，可以注册一个“查询工单”工具，在适配器中调用现有工单 API，并按项目的身份和数据权限限制可查范围。模型选择工具、记录调用结果和回放执行过程的逻辑可直接复用。[场景接入指南](docs/scenarios.md)

首版可在本机运行。随附的网页供开发者切换场景、提问、核对证据和调试 Trace。「资料问答」与「文本分析」是两个可独立移除的示例场景。

## 启动

需要 Python 3.11 或更新版本。在 Windows 上进入仓库目录，执行 `.\start.cmd`。首次启动会创建虚拟环境并安装依赖，需要能访问 Python 包源。打开 <http://127.0.0.1:8765>。个人模式只监听本机。按 Ctrl+C 停止服务。

其他系统，或已有 Python 环境时，可使用以下命令。

```sh
python -m venv .venv
# macOS / Linux
. .venv/bin/activate
# Windows PowerShell 则运行 .\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python run.py
```

1. 选择「资料问答」，点击「使用示例」再运行。展开证据可看到样文标题、版本和原文片段。
2. 切换到「文本分析」，粘贴一段文本或使用示例。结果包含字符数、非空行数和高频词项。中文示例用空格分隔词项，连续的汉字计为同一词项。
3. 展开右侧执行轨迹查看工具、权限、输入摘要和耗时。刷新网页后，在「最近运行」中重新打开任务。继续提交会沿用当前会话，点击「新会话」开始独立对话。

## 离线与模型模式

**离线演示**无需密钥。每个场景在清单中指定一个工具，工作台把输入文本交给它。资料问答使用本地词项匹配，回答来自命中的样文。两个示例均可独立移除。

**模型模式**使用支持工具调用的 Chat Completions 兼容端点。将 `.env.example` 复制为 `.env`，设置服务的 API 基址（含实际路径前缀，如 `/v1`）和模型名，重启后在网页切换模式。

```ini
AGENT_MODEL_BASE_URL=http://127.0.0.1:8000/v1
AGENT_MODEL_NAME=填写本机服务中的模型名
AGENT_MODEL_API_KEY=
```

远程服务需配置 HTTPS 和服务端密钥。本地回环端点可省略密钥。模型模式会向该端点发送当前问题、当前会话的有限历史和所调用工具的结果。模型可发现能力、执行工具、检索资料，工作台将本轮来源编号为 `[E1]`、`[E2]` 等。模型返回错误或超时会记录在任务与 Trace 中。实际兼容性取决于端点能否处理工具调用和工具结果消息。

## 为自己的项目复用

复制一个场景目录，修改清单、输入输出 Schema 和适配器，放入 `scenarios/` 后重启。也可让 `AGENT_SCENARIOS` 指向你自己的独立目录。配置可以为空；工作台仍可启动并展示空状态。场景包支持本地 Python 工具、固定目标的 HTTPS 工具、可选本地资料和权限码。[场景接入指南](docs/scenarios.md)

前端或其他程序使用 REST 与 SSE。开发配置可设 `AGENT_ACCESS_TOKEN` 为自己的高熵令牌；服务重启后，网页会要求输入令牌。下例从调用端环境读取同一个令牌。

```powershell
$headers = @{ Authorization = "Bearer $env:AGENT_ACCESS_TOKEN" }
$body = @{ business_id = "notes"; message = "如何记录会议纪要？"; mode = "offline" } | ConvertTo-Json
Invoke-RestMethod http://127.0.0.1:8765/api/v1/agent/chat -Method Post -Headers $headers -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($body))
```

```sh
curl -N http://127.0.0.1:8765/api/v1/agent/chat \
  -H "Authorization: Bearer $AGENT_ACCESS_TOKEN" -H "Content-Type: application/json" \
  -d '{"business_id":"notes","message":"如何记录会议纪要？","response_mode":"streaming"}'
```

[API、Trace 与存储](docs/api.md) · [验收记录](docs/verification.md)

## 测试

```sh
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

测试使用临时 SQLite、可移除的示例和模拟的模型 HTTP 响应，可在没有模型账号时复现。
