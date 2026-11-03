# 执行记录 — docs/v3_1/plan.md

- Task 1: complete — 基线 ac448df；387 passed、12 subtests，181.04秒；新增快照测试1 passed，pip check通过。
- 设计复核：v31_builder_check 无需用户决策的阻断问题。V31-020 必须区分工件完整性/签名检查与未提供的安装路径，最终不得把 inspect 当安装测试。
- Ruling: 在已授权的P0/P1范围内直接实施，不追加形式批准；保留原API/SQLite/SkillOps，并默认关闭新编译器。
- Pre-flight: Task2的输入/输出不依赖DevKit；Task3使用既有包解析；Task4通过既有SkillOps/Gateway使用两者，没有第二套发布状态或执行状态。
- Task 2: complete — 编译器8个新行为先失败后通过；Provider接入2失败2通过后修复；同来源不同data_as_of证据2个用例先复现重复E1，再修复。上下文/模型/旧技能/兼容专项49 passed，14.19秒。最终全量待Task4。
- Ruling: 证据去重键加入 source_ref/data_as_of — 原键会覆盖同版不同时点的证据，违反P1保留时点要求 — 此类结果可能增加证据数/改变后续引用编号，接口字段不变。
- Ruling: Store.finish 原本会清理 calls.result，测试改在实际 Gateway 返回边界核验完整结果，不为满足测试改变原保留策略。
- Task 3: complete — DevKit初始16失败，补充畸形输入3失败、依赖诊断1失败，修复后新旧技能专项59 passed，35.22秒。context_compiler、skill_devkit、agent CLI三模块mypy通过；未宣称全库类型零错误。
- Task 4: complete — 第三项目先因文件缺失失败，补齐后验证实际Gateway与原发布API。最终430 passed、12 subtests passed，196.68秒。三模块带check-untyped-defs的受限mypy、compileall、node --check、pip check通过；普通mypy遍历依赖有11项存量/缺少stub问题，未声称全库通过。
- 最终复核：发现缓存源能力撤销漏检和离线Schema验证无时间限额，分别复现并修复；专项20和24 passed，最终全量覆盖新增provider来源记录测试。复核服务未完成最终结论，主审查及本机验证不冒充独立审计全通过。
- Ruling: V31-020完整项NOT_RUN，摘要/签名检查子项PASS；P2/P3/P4新能力NOT_RUN，具体48项映射见acceptance.md。第三项目使用SkillRegistry API建立测试发布，未把它说成工件安装或独立人员审核。
- 输入比较：固定虚构样本估算49155→1165，2条证据与源结果不变；真实模型Token/性能/语义质量NOT_RUN。
