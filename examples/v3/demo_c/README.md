# 第三个独立项目：虚构集合计数

所有数据均为虚构。C-01有3件、C-02有5件，演示账号只能读取这两个集合；C-03用于越权拒绝测试。核心代码不包含该业务的ID或条件分支。

在仓库根目录，用已安装锁定依赖的Python执行：

```powershell
python scripts/agent.py skill validate examples/v3/demo_c/skills/collection-count --capabilities examples/v3/demo_c/capabilities.json
python scripts/agent.py skill test examples/v3/demo_c/skills/collection-count --offline --capabilities examples/v3/demo_c/capabilities.json
python scripts/agent.py skill pack examples/v3/demo_c/skills/collection-count --out work/demo-c-dist --capabilities examples/v3/demo_c/capabilities.json
python scripts/agent.py skill inspect work/demo-c-dist/collection-count-1.0.0.zip
python -m pytest tests/test_v31_demo.py -q
```

pack 不覆盖已有同名ZIP，重复验证可用新的输出目录。工件检查不授予权限也不安装。上面的 pytest 将项目复制到临时外部目录，使用原 SkillRegistry API建立测试发布、真实 SQLite 和 Gateway、确定性 Mock 模型，验证实际工具调用、证据、权限拒绝、幂等和 SSE 重放；不代表独立人员审核或工件安装验证。

长期本机接入沿用 examples/v3/README.md 的场景目录、身份配置与 skillctl 流程；本技能须将 `demo.collection.count@1.0.0` 显式绑定到 `count_items`，并为用户配置 `collections.read` 和明确的对象集合。不可把 capabilities.json 当成用户运行权限。
