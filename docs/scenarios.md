# 接入一个场景

每个场景占用一个目录。目录名与清单 `id` 一致，使用小写字母、数字和下划线，以字母开头。后端启动时校验清单、版本、工具名、适配器引用和输入输出 Schema，错误会带上目录位置。网页从场景清单读取名称、简介和示例。场景清单也承担首版的 Skill 配置职责。

若要验证第三个场景，可复制 `scenarios/text` 为 `scenarios/my_text`，将清单的 `id` 改为 `my_text`、`name` 改为「我的文本工具」，重启后选择新场景运行。工具名按场景隔离，无需修改编排器或网页。移除目录并重启即卸载。SQLite 中的历史保留，同名场景的版本号会记录在各次任务中。

## 数据与执行

- 清单的 `tools` 数组描述工具的 `name`、`description`、`permission`、`action_level`、`input_schema`、`output_schema` 和 `adapter`。工具输入、输出使用内联 JSON Schema，对象必须设置 `additionalProperties: false`。支持的关键词见注册校验器和 [Manifest Schema](scenario.schema.json)。
- `offline_tool` 是可选的离线入口，接收 `{"text":"..."}` 并返回 `data.summary`。模型可以选择场景中的其他工具，入参可以有不同结构。涉及绑定资源的工具可声明 `object_ids_field`，把入参中的 ID 数组限制到任务已授权对象范围。
- `adapter: {"type":"python","handler":"statistics"}` 会加载同目录 `adapter.py` 里的函数。处理器接收 `(arguments, retrieve)`，可同步或异步。返回 `{"data": {...}, "evidence": [...]}`。本地适配器是有本进程权限的受信任代码，只安装你审核过的包。
- `adapter: {"type":"http","url":"https://your-service.example/tools/count","method":"POST"}` 会把参数发往清单中的固定地址，读取同格式响应。支持 GET 或 POST，拒绝重定向。既有服务若返回其他协议，先用受信任适配器做转换与凭据管理。
- 资料文件与清单同目录，由 `documents` 指定。每条记录有 `source_id`、`title`、`snippet`、`version`。每个资料文件属于一个场景，调用 `retrieve(query, top_k)` 搜索当前场景。本地示例按场景授予整份资料读取权限。未来接入按用户分权的外部资源时，适配器应使用已核验的对象范围和服务端权限查询数据。
- V1 允许 `read` 与只生成草案的 `propose_write`，禁止执行 `commit_write`。开发者负责保证所接入服务的真实行为与声明一致。

完整的两种处理器可见 [资料问答](../scenarios/notes/adapter.py) 与 [文本分析](../scenarios/text/adapter.py)。将场景、工具、数据和权限变更一同纳入版本控制，修改行为时同步更新场景版本。
