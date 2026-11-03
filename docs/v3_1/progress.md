# 执行记录 — docs/v3_1/plan.md

- Task 1: complete — 基线 ac448df；387 passed、12 subtests，181.04秒；新增快照测试1 passed，pip check通过。
- 设计复核：v31_builder_check 无需用户决策的阻断问题。V31-020 必须区分工件完整性/签名检查与未提供的安装路径，最终不得把 inspect 当安装测试。
- Ruling: 在已授权的P0/P1范围内直接实施，不追加形式批准；保留原API/SQLite/SkillOps，并默认关闭新编译器。
- Pre-flight: Task2的输入/输出不依赖DevKit；Task3使用既有包解析；Task4通过既有SkillOps/Gateway使用两者，没有第二套发布状态或执行状态。
- Task 2: complete — 编译器8个新行为先失败后通过；Provider接入2失败2通过后修复；同来源不同data_as_of证据2个用例先复现重复E1，再修复。上下文/模型/旧技能/兼容专项49 passed，14.19秒。最终全量待Task4。
- Ruling: 证据去重键加入 source_ref/data_as_of — 原键会覆盖同版不同时点的证据，违反P1保留时点要求 — 此类结果可能增加证据数/改变后续引用编号，接口字段不变。
- Ruling: Store.finish 原本会清理 calls.result，测试改在实际 Gateway 返回边界核验完整结果，不为满足测试改变原保留策略。
- Task 3: pending。
- Task 4: pending。
