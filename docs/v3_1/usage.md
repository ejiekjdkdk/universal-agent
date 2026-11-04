# V3.1 首轮使用方式

本轮为现有 V3 的可选增强，P2/P3/P4 新能力尚未完成。Python环境、启动网页及原 SkillOps 用法保持不变。

## 上下文编译

在本机 .env 设置 `AGENT_CONTEXT_COMPILER=1` 后启动 `python run.py`。默认0保持旧行为；确定性 Mock 与模型模式共用新编译流程，无技能的旧 offline 直达工具路径不需要模型输入。

嵌入式调用可通过 `create_app(model_settings={..., "context_compiler": True, "context_window_tokens": 65536, "context_output_reserve": 2048, "context_safety_margin": 1024})` 设置本地预算。这些数值是保守上限，不是任何真实模型的窗口认证；UTF-8字节数加封装余量仅用于保守估算，真实Tokenizer/用量/性能待目标模型验证。

系统策略、选中技能、当前目标、任务状态和全部本轮证据保持必需。可选历史可省略，大工具明细可换成带完整证据和随机引用的确定性摘要；每轮重新鉴权，原文读取也重新鉴权且占用原模型工具次数。原文仍放不下时明确失败。请求内引用不跨任务、不跨重启保存；原Store的终态清理策略没有改变。

编译记录放在已有audit_logs中，action=context.compile，resource为不含Prompt原文的JSON报告；没有新增公开SSE事件或结果查询API。结构化结果的limitations说明省略。未绑定技能的旧自由文本输出不新增字段；其编译省略报告可在内部审计检查。

## 技能开发

```powershell
python scripts/agent.py skill init work/my-skill --business notes
python scripts/agent.py skill validate work/my-skill
python scripts/agent.py skill test work/my-skill --offline
python scripts/agent.py skill pack work/my-skill --out work/dist
python scripts/agent.py skill inspect work/dist/my-skill-1.0.0.zip
python scripts/agent.py doctor --scope local
```

init 与 pack 不覆盖已有目标。清单声明能力依赖时，validate/test/pack 必须额外传 `--capabilities <可信JSON文件>`，内容为批准的 `ID@VERSION` 列表；这不授予运行权限。只接受受限UTF-8文件，不执行任何技能脚本，不联网。

fixtures 放在 tests/*.json，每个文件为列表，单条形如 `{"id":"missing-message","target":"input","value":{},"valid":false}`。target只能是input/output，value按对应Schema验证，valid是期望的合法性。缺失fixtures或Schema写NOT_RUN且退出非零。PASS仅表示声明式契约样本符合预期，不证明业务语义或真实模型效果。

离线合同测试在独立进程执行，启动后等待上限10秒；超时返回 `FAIL/VALIDATION_TIMEOUT` 并终止和回收工作进程。复杂Schema或速度较慢的机器可能触及时限，需要简化样本或Schema，不能把超时当作通过。这是执行时限，不是操作系统资源沙箱。Python程序直接调用时需按Windows多进程惯例放在 `if __name__ == "__main__"` 保护下；官方CLI已处理。

候选工件使用固定ZIP元数据、文件摘要、依赖锁和包摘要。可在本地环境变量中配置至少32字节的独立工件密钥，并为pack/inspect显式传 `--signing-key-env <变量名>`。密钥不写入工件。未提供验证密钥时，只能报告unsigned/unverified，不得当成可信发布签名；本机HMAC不是PKI。

inspect只检查，不安装，不发布。原目录导入发布继续使用 scripts/skillctl.py；发布时仍需作者、审核者、工具绑定和固定评测。不要把ZIP检查成功当成已审核安装。第三项目的完整演示见 examples/v3/demo_c/README.md。

## 兼容性与限制

- API字段、调用参数、SSE及数据库结构由自动快照检查和旧回归验证。
- 证据去重增加来源地址与数据时点：同来源同版本但不同时点现在保留为不同证据，后续引用编号可能相应改变，这是有意修复丢失时点的缺口。
- 本页描述P0/P1。后续的加密结果库和缓存见 [P2说明](p2-usage.md)，可信Provider切换和显式本地Replay见 [P3说明](p3-usage.md)。这些本机能力不代表生产发布已完成。
- 候选包凭据扫描只覆盖明确格式，不能识别所有语义上的秘密。不要将真实业务资料作为开发fixtures。
- 关闭编译开关即可回到旧组装路径；无需数据库迁移或删除历史任务。
