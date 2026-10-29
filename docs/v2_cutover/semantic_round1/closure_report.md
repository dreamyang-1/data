# Semantic Production Root Cause Closure — Round 1

**SEMANTIC_ROOT_CLOSURE_ROUND_1_COMPLETE**

**NO_GENERIC_OPERATION_DEFECT_PROVEN**，结论范围仅限本轮原始13条 `V2_EXPLICIT_OPERATION_DROPPED`。这13条全部在首轮初始化时被安全拒绝，未进入 TaskPatch 编译、Reducer 或状态发布。可疑证据是识别阶段额外声明的 `projection_spec`，并非已经明确且可绑定的用户编辑操作被 Reducer 丢失。没有修改生产代码，没有提高任何正式 Gold 分数。

## 1. 基线、分支和发布

唯一基线为 PR [#54](https://github.com/dreamyang-1/data/pull/54) 的 `6cc55e217574a68d7faa66e6d4c4de4c101341c9`，基线分支 `pre-semantic-calibration-20260909t111841z`。开始时核对本地/远程 HEAD、PR open/draft/unmerged、1448项 tracked 文件及工作树，未发现 drift 或遗留运行脚本。见 `starting_state.json`。

本轮唯一分支 `semantic-operation-round1-20260909t122901z`，一个堆叠 Draft PR，base 为上述基线分支。最终提交通过 `git_commit_manifest.json` 的 introducing-commit 命令精确解析，发布后核对远程 SHA 与 Draft PR 的 head/base。该方式避免提交将自己的 SHA 写入自己的树。不合并、不切流。开发目录 `E:/YouoAgent/DataAnalysis_Agent`，显式同步版本仓 `E:/yy`。

## 2–5. 原始13条与因果重分类

完整逐条 Operation / Operand / Target / Edit / Reducer / State / IR 证据在 `operation_case_inventory.json`，索引在同名 CSV。真实调用链在事前完成的 `operation_dataflow_map.md`。原始 Public 11条：G81-001、003、008、009、010、012、058、059、060、061、066；原 Private 2条：PV81-010、016。

| Subcluster | Case IDs | 数量 | Operation / Operand / Target 分类 | 唯一 First Divergence |
|---|---|---:|---|---|
| OP-R1-ENTITY-PROJECTION | G81-001/003/008/012 | 4 | TARGET_SLOT_UNRESOLVED | SEMANTIC_ROLE |
| OP-R1-PRESENTATION-CUE | G81-009/010/058/059/060/061、PV81-010/016 | 8 | OPERAND_UNRESOLVED | SEMANTIC_ROLE |
| OP-R1-MERGED-BOUNDARY | G81-066 | 1 | OPERAND_UNRESOLVED | MENTION_BOUNDARY |

第一组把实体列表的 subject 同时声明为显式 projection ADD。第二组把“名单／汇总值”等展示或聚合表达声明为可绑定字段。第三组将实体与列表表达合成一个 surface，并据此声明投影。其第一因果边界分别是 `CurrentTurnSemanticParse` 的 slot/operation 声明、mention role 与对应声明、mention surface 边界；共同的最后拒绝点才是 `RawTurnPlanner._patch: markers - used_markers`。

这里 TARGET_SLOT_UNRESOLVED 表示实体已作为 subject 成功绑定，但附加投影角色不成立；OPERAND_UNRESOLVED 表示没有合法的投影字段对象。不是把一个合法 REMOVE 操作因未找到成员而改称识别失败。13条均保留合法 subject/metric 初始化操作的 CURRENT_EXPLICIT 证据；附加 projection 没有相应合法 edit。

A OPERATION_EVIDENCE_DROPPED=0，B OPERAND_UNRESOLVED=9，C TARGET_SLOT_UNRESOLVED=4，D TARGET_TASK_UNRESOLVED=0。每组有相同第一边界、不变量和消费路径，详见 `operation_root_subclusters.json`。3组是下一轮假设实验的调查分组，不宣称3个独立模型 Bug。

8条为真实单轮；G81-058/059/060/061/066 的失败发生在 history 的第0轮，随后计划执行的当前轮尚未运行。不能把这5条算作已执行的追问 ADD/REMOVE/CLEAR 失败。所有13条当前关系均为 NEW_TASK，target 为 `task:turn-0`，没有可继承旧状态。

另外通过冻结目录与原生 `ScopedPlanSession/default_projection` 独立只读验证了 product_line、city、manufacturer、project、sales_company、province 的受治理主显示属性，6/6有合法默认 projection。证据见 `catalog_default_display_evidence.json`。这只证明默认投影存在，不证明模型选择的 RELATION_LIST 合法。

## 6–9. 风险、Oracle 和根因归属

| 风险 | 原始 Case |
|---|---:|
| CORRECT_ACCEPT | 0 |
| SAFE_REJECT | 13 |
| WRONG_CLARIFICATION | 0 |
| UNSAFE_ACCEPT | 0 |
| WRONG_SILENT_ACCEPT | 0 |

每条均先在原始冻结 capture 上严格回放，然后启用只读拒绝帧观察并核对结果：13/13 instrumentation parity。观察器读取真正 `_patch` 的帧，使用 unwrap 排除现有 RuntimeObserver 包装；不更改返回值或异常。原始 capture hash、候选集、目录、Scope、as_of 均保持冻结。

| 实验 | 唯一干预 Artifact | 结果 |
|---|---|---|
| 原始 Baseline | 无 | 13/13 V2_EXPLICIT_OPERATION_DROPPED |
| MARKER_ONLY | 仅去除 Parse 中未消费 projection marker | 13/13 V2_EXPLICIT_SLOT_DROPPED |
| DECLARED_SLOT_PROPOSITION | 同一个 Parse 中去除 projection marker 与对应 explicit_slot_mentions 声明 | 3个诊断性接受，6个 CATALOG_RELATIONSHIP_REQUIRED，4个 V2_QUERY_SHAPE_CONFLICT |

后一实验保留 mentions/roles、relation signals、target、binding、第二阶段 SemanticTaskDraft、query shape、候选和 Scope。两个模型阶段的 output_changed 恒为 `[true, false]`；第二阶段输入因单一 Parse 干预可能改变，但它的模型输出保持原 capture，不重新调用模型。实验不是可部署的忽略 marker 规则。

诊断接受的3条为 G81-003、PV81-010、PV81-016。其余6条为 G81-001/008/010/012/060/061，4条为 G81-009/058/059/066。后10条揭示后续 query-shape 约束；尤其缺 relationship 可能由错误选择 RELATION_LIST 触发，不能据此宣布 Catalog 有缺陷。它们只记 downstream_effects，不为同一 Case 重复计根因。

结论 **CAUSAL_EVIDENCE_CONFIRMED** 限于“附加投影声明导致本轮被拒绝”：移除声明后，该边界在13/13消失。并未证明仅修复该声明即可让13条全部正确。证实的通用 Operation Contract defect=0。重分类去向：SEMANTIC_ROLE 12，MENTION_BOUNDARY 1；TURN_RESOLUTION、CANONICAL_BINDING、ENTITY_VALUE_GROUNDING、IR 均0条 primary。所有正式 Case 仍 FAIL。

本轮没有发现新的评分器缺陷；从 reason-code 拒绝点分组细化为因果分组属于审计，不修改冻结 Evaluator 或历史报告。不得将这次重分类当作准确率提升。

## 10–11. 修改范围与最小性

新增 `tools/cutover/operation_round1.py`，只提供离线单 Artifact Oracle 与只读拒绝帧诊断；新增 `tests/test_v2_operation_round1.py`；新增本目录报告、证据和只读验证入口。精确文件清单与 hash 在 `change_manifest.json`。

生产修改=0；Validator 放宽=0；新增架构层/Operation enum=0；Prompt、Schema、Semantic Regex、业务关键词、Few-shot、Evaluator 修改均0；Oagnet/SQL Translator 修改=0。没有制造生产补丁，因为现有证据没有证明 Operation 消费合同缺陷。不能把 Oracle 的删除声明直接投入生产，这会掩盖真正遗漏的用户操作。

## 12–15. 原子性、版本、Barrier、Provenance

新增测试走真实 RawTurnPlanner、canonical binding、TaskPatch、Reducer 和 State Machine，模型 HTTP 输出受控。混合合法 ADD/REMOVE 原子成功；缺失成员、未提供 binding handle、重叠编辑、structured/direct slot 冲突均按明确 reason 拒绝，输入 state、message ledger、task/state version 与 barrier 不变；同 message 的合法重试只应用一次。

发生语义变化时 task version 递增；重复 canonical ADD 等语义 no-op 不增加 task version，成功接收新轮次仍增加 state version。CLEAR 和 REMOVE-last 均形成已有 filter inheritance barrier，后续继承、换时间、加指标不会恢复旧地区。当前 patch 与 structured edit trace 保留 CURRENT_EXPLICIT，继承的 metric 引用保留原 turn0 来源。

独立 slot 的 CLEAR filter + ADD metric 顺序不改变最终语义。相同 filter target 的无序 CLEAR+ADD/REPLACE 拒绝重叠；既有 Phase2.5.1 declarative TaskPatch 阶段合同允许的 CLEAR+ADD 不变。不能把低层已定义的执行阶段误当作新的用户歧义裁决。

## 16–17. Operation 负迁移与 Safety

| 原始未消费模型 marker | Before FAIL | After FAIL | New PASS | New FAIL | Reclassified | BLOCKED |
|---|---:|---:|---:|---:|---:|---:|
| ADD | 6 | 6 | 0 | 0 | 6 | 0 |
| REPLACE | 0 | 0 | 0 | 0 | 0 | 0 |
| REMOVE | 0 | 0 | 0 | 0 | 0 | 0 |
| CLEAR | 0 | 0 | 0 | 0 | 0 | 0 |
| SET | 7 | 7 | 0 | 0 | 7 | 0 |

分母是原始13条中未消费的模型 marker，不是用户真正发出的编辑命令；业务行为都是初始化（subject 11、metric 2）。零样本不算 PASS。其余操作用受控集成测试独立验证，不与 Gold 分数混算。详见 `operation_negative_transfer.json`。

新增23项测试=21项原生受控 Safety +2项 Oracle 单 Artifact 检查，全部通过。覆盖 CLEAR/REMOVE-last 三类后续轮次、重复 ADD、single→single/multi REPLACE、混合 ADD/REMOVE/CLEAR、缺 operand、绑定非法、原子性、版本、provenance、独立顺序、冲突、业务操作词新任务、否定查询、跨 Scope 和最小真实 Pending 操作入口。见 `operation_safety_matrix.json` 的精确 nodeids。

Pending 验证仅证明当前不满足 expected-answer 的 REMOVE 不会被强制作为精确答案，也没有修改旧状态；不宣称完整 Pending 业务链已就绪。新跨 Scope 用例验证 knowledge-base 边界；原 Critical Scope 套件继续验证 model/domain 边界。

新测试不能替代真实模型识别安全证据。既有 live operation Safety 总门禁仍 INCOMPLETE，Dataset ranking 与主链 DryPlan 缺口仍在；没有将0/0安全轴标 PASS。

## 18–20. Full Plan、Private 与 Blind

Certified Full Plan Gold 16条、22个原始执行轮严格回放，原可观察13轴的冻结评分对象完全一致。Whole Plan 仍16 BLOCKED：主链 DryPlan 未观察，**WHOLE_PLAN_PASS=0**。本轮没有补 Dataset Adapter、主链 DryPlan、SQL 或 Catalog Relation。见 `full_plan_observable_delta.json`。

PV81-010、PV81-016 在读取原句及完整 capture 前已 PROMOTED_TO_DEV，记录 promotion 时间、理由及 subcluster；旧数据和历史报告不重写。PRIVATE_VALIDATION_ORIGINAL=24，DEV_PROMOTED_FROM_PRIVATE=2，PRIVATE_VALIDATION_REMAINING=22。

剩余22条只作整体评估：35轮严格回放，PASS=0、FAIL=22、NOT_RUN=0、BLOCKED=0，计划/状态或同样安全拒绝与冻结记录一致。未逐条调试、未将其原句或模型完整输出公开，未把提升的2条继续计入 Private 泛化结果。见 `private_remaining_evaluation.json`。

Blind Holdout 保持封存，原句查看/运行/调试/模型调用均0，仅继承既有 hash manifest。真实模型调用=0、源 SQL=0、生产外部写入=0。本轮没有确定性生产改善，因此不进入 Live rerun、正式 Model Benchmark 或 Shadow。

## 21–22. 回归和复审

Focused baseline 259 passed；新增23项测试自审后通过；全量 Agent 从3160 passed /27 failed 到 **3183 passed /27 failed**；新增测试23，collection errors=0，old-pass→new-fail=0，old-fail→new-pass=0，旧 node 缺失=0，旧 expectation 修改=0。

Critical 160/160 来自本轮全量离线回归的原有精确三文件选择；已观察到的89条公开追问均有 reason Trace。Oagnet 和 SQL Translator 源码无变动，本轮未重跑其全量测试，不冒充新结果。完整失败 nodeids、计数与结果 hash 在 `test_delta.json`。

复审检查了观察器不改变拒绝、Oracle 只改一个 Artifact、原 capture 与第二阶段输出不变、Private promotion 时点、全13没有 Reducer、默认显示目录事实、零样本分母、拒绝原子性和合法重试。修正过诊断观察器的 wrapper-frame 定位，并重新完成13条 Oracle；没有修改冻结 Evaluator。父阶段 Harness/Calibration 验证器继续 PASS。本轮验证入口为 `python docs/v2_cutover/semantic_round1/verify_round1.py`，无模型、无私有原句读取、无输出文件写入。

## 23–24. 剩余根因与停止点

原13条中仍归 Operation consumer 的已证实根因 Case=0；重分类后仍未解决的 recognition Case=13。其他8条 TASK_OPERATION 不在本轮调查范围，不能宣布全部 Operation P0 清零。13条为 P1 safe-reject 阻塞；本轮没有证实 silent-wrong-query P0。

下一候选仅登记为原优先表第2项 `TURN_RESOLUTION / V2_EXPLICIT_SLOT_DROPPED`（覆盖上限12），必须先因果证明，不能从名字断言要改 TurnResolver。本轮新增的 recognition 投影过度声明证据作为后续排序输入，不自动修 Prompt 或业务词规则。

V1 正式路由、API/SSE、生产默认模型和 Scope 合同保持不变。Evaluation 的其余语义失败、live Hard Safety 覆盖、Dataset/DryPlan 能力及 V1/V2 同分母比较仍约束替代就绪；目录正式发布和 Redis 恢复证据继续约束 Canary/Cutover，不重新阻塞全部离线评测。当前不是 READY_FOR_USER_APPROVAL。

按本轮停止条件，完成提交和一个 Draft PR 后停止，等待下一阶段指令。
