# V3 P4 本地 SkillOps

本实现交付本地发布治理闭环，不接入生产系统。Skill 是方法说明；激活不授予业务权限。Runtime 每次调用仍核验当前身份、工具范围、能力撤销与绑定的 Skill 快照。

## 状态与证据

`import → draft → validate → validated → stage → staged → activate → active → deprecated`。`rollback` 只能将通过既有门禁的 deprecated 版本恢复 active；`revoke` 可紧急阻断任意已有发布，旧任务再次加载正文或引用时同样被阻断。draft、validated、staged 均不参与路由。

包内容及引用、Schema、manifest 在 SQLite 中冻结；相同名称版本的不同内容不能覆盖。每个发布记录作者、独立审核者、包 digest、内容摘要、schema_version、精确能力版本映射、evaluation_run_id、release_snapshot_id、报告摘要和 HMAC-SHA256。验证/暂存/激活/加载均核验相应凭证。包内声明不会生成真实发布权限，CLI 拒绝伪造 active 状态、签名或审核凭证。

作者来自受信任的本地导入操作，审核者必须不同。HTTP 管理接口从服务端管理员身份取得 reviewer，拒绝请求附带 reviewer。CLI 作者和审核者是本地操作员声明，并非企业人员身份认证。默认兼容 API 使用 `local-author` 与 `local-reviewer`；`publish` 仍依次执行完整门禁。

HMAC assurance 明确标为 `local-hmac-sha256`。密钥由 `AGENT_SKILL_SIGNING_KEY` 提供，至少 32 字节；未配置时生成注册表目录中的 `.operator-key`，重启复用。应保护该目录和密钥。此机制检测未持有密钥的内容或凭证篡改；它不是第三方代码签名、独立审批系统或防本地管理员篡改的证明。更换密钥会使旧凭证失效，不能删除密钥后继续使用旧发布。

## 固定评测

每次 validate 运行 `skillops-local-v1`，报告内容确定，独立运行 ID 不同。用例调用实际 Runtime/Gateway 与 Store 检查注入工具调用阻断、跨租户访问、重复提交、循环计划、预算增加；同时读取已冻结 Skill，扫描显式恶意指令，验证未绑定业务无法加载。失败报告及运行 ID 持久化，门禁拒绝发布。扫描只是有限检测，不宣称覆盖所有提示词注入。

`compare` 返回两个版本的固定套件一致性、退化用例、精确能力映射兼容性和两次运行 ID。业务答案的正确性由现有场景金标发布评测负责；这个门禁不宣称测量任意 Skill 的语义质量或生产成功率。

## 操作示例

以下命令在仓库目录运行；`--author` / `--reviewer` 属于顶层参数，放在子命令前。

```powershell
python scripts/skillctl.py --root .local/skills --author alice import examples/v3/demo_a/skills/record-lookup
python scripts/skillctl.py --root .local/skills --reviewer bob validate demo_records record-lookup 1.0.0 --scenarios examples/v3/demo_a/scenarios --tool read_record
python scripts/skillctl.py --root .local/skills --reviewer bob stage demo_records record-lookup 1.0.0
python scripts/skillctl.py --root .local/skills --reviewer bob activate demo_records record-lookup 1.0.0
python scripts/skillctl.py --root .local/skills details demo_records record-lookup 1.0.0
python scripts/skillctl.py --root .local/skills audit --business-id demo_records
```

上例使用仓库自带的 Demo A。带 required_capabilities 的扩展包还须通过 --bind-capability ID@VERSION=TOOL 显式绑定每个精确版本。CLI 的 `publish` 为 validate/stage/activate 的便利入口；读取型工具须存在于指定受信任场景目录。API 不开放任意服务器文件路径导入：先用本地 CLI 导入，再管理状态。

管理路由均需独立管理员令牌，前缀 `/api/v1/agent-admin/skills`：POST `validate`、`stage`、`activate`、`rollback`、`revoke`；GET `audit` 和 `/{business_id}/{name}/{version}`。状态请求包含 business_id/name/version；validate 还包含 tools 和 capability_bindings；activate 可传 canary_percent。请求不能自报状态、签名或评测结果。

## 灰度、回滚与吊销演练

1. 发布 1.0.0 为稳定版本，导入并 validate/stage 1.1.0。
2. `activate ... 1.1.0 --canary-percent 20`。需要另一个 active 稳定版本。按 tenant/org/user 与业务和 Skill 的固定摘要分桶；相同主体重启后仍命中相同版本。
3. 新任务通过 `active_for(actor, business)` 仅选择一个版本；已存在的任务使用原 snapshot，版本提升后仍可加载 deprecated 内容。
4. 将 1.1.0 激活到 100%，1.0.0 变 deprecated。`rollback ... 1.0.0` 恢复旧版路由；不重新执行已完成任务，也不改写已绑定的内容。
5. `revoke ... 1.1.0` 后，仍固定在 1.1.0 的旧任务立即拒绝后续 Skill 加载/引用读取；被吊销版本不能回滚恢复。吊销灰度版本时新任务回到稳定版本。

## P1 升级

首次打开旧注册表时，缺少治理凭证的 active/deprecated 发布变为 `legacy_pending`，不会自动伪造审核证据或继续路由。包和原绑定保留。运维检查旧内容及可信场景目录后，显式执行：

```powershell
python scripts/skillctl.py --root .local/skills --author alice --reviewer bob migrate BUSINESS SKILL VERSION --scenarios PATH_TO_TRUSTED_SCENARIOS
```

迁移重新执行同一固定评测、独立作者审核、签名、暂存和激活。先迁移历史保留版本，最后迁移希望成为稳定版本的版本；每次激活会将其他 active 版本改为 deprecated。已有 revoked 版本不能迁移复活。审计表持久记录导入、评测、validate、stage、activate、rollback、revoke；HTTP 失败操作另进入主 Store 的管理审计。

## 验证

```powershell
python -m pytest tests/test_skill_governance.py tests/test_skill_packages.py tests/test_harness_skills.py -q
```

覆盖原 P1 包与 Harness API、独立审核、门禁顺序、可重复评测、签名/正文/引用/报告/绑定篡改、灰度重启稳定性、旧快照及实时吊销、CLI 伪造状态、旧包显式迁移、管理员路由。Windows asyncio 创建本地 socketpair；受限沙箱可能阻止事件循环启动，需在允许本地回环的测试环境运行。

## 冻结的 Skill 执行契约

可选 `execution` 只支持 `trust_level: instructions_only` 和 `allow_scripts: false`；`max_tool_calls` 为 1–32 的整数，缺省为 8。Runtime 从已签名快照读取该限制并与全局预算共同执行。包内 scripts 不会执行。声明 `business_scopes` 时，发布业务必须包含在非空列表中。

可选 `input_schema_ref` / `output_schema_ref` 必须指向冻结的 schemas/*.json。平台固定使用 JSON Schema Draft 2020-12，不按根 `$schema` 声明切换方言。同一文件内的 `$ref`、`$dynamicRef` 按该方言处理；`$ref`、`$dynamicRef`、`$recursiveRef` 的外部目标均被禁止，不会加载网络 Schema。旧 `$recursiveRef` 不是当前校验器支持的约束，不应依赖它实施输入或输出校验；请在新版本技能包中改用受支持的定义。本轮保留已有包的导入和加载规则，未来收紧方言或关键字门禁前须先盘点旧包并确定迁移方案。Runtime 契约固定为：

- 输入：`{"message": "用户消息", "context": {"object_ids": ["对象编号"], "object_type": "可选对象类型"}}`。object_type 未指定时可以省略。
- 输出：最终结构化结果对象，字段为 `answer`、`evidence`、`limitations`、`unresolved_items`、`artifacts`。

未声明对应 Schema 时，该项不增加 Skill 专属校验。工具自身输入/输出 Schema 和实时权限检查仍独立生效。
