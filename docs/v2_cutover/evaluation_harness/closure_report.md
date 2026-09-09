# V2 Evaluation Harness Closure

**EVALUATION_HARNESS_NOT_READY**

本阶段完成了真实 V2 RawTurnPlanner 观察、版本化重评、typed Pending/Dataset fixture、严格回放和评测器自测；尚未达到完整 Harness 关闭门禁。V1 保持正式路由，未修改生产语义决策、Prompt、Schema、模型配置或公共 API/SSE。

基线为 PR #52 / `ec48f654d6bcaa48ed09d41d7aca4fcbaedcd82e`。实际模型采集入口代码提交为 `871183e62ada81c5526e51948a7e11ba2d3c8908`，最终评测器提交为 `f6ce319591d714d658916f48cdc6fe7c70d9b252`，hash `3ac9f973590abfdeb3d233ea501accca8f6dceb2ff7f4b6f1955e599a896cbf0`。两者的生产语义源码、Prompt、Schema 完全相同。评测器复审期间生成的中间分数仅保存在 PRIVATE；本报告只引用最终冻结评测器对原始记录的严格重评。完整 PR #1→#52 依赖链、阶段提交、快照与模型参数见 `evaluation_baseline_manifest.json`。

## 1. Runtime / Evaluator Parity

完整 17 项 Artifact Matrix 见 [runtime_evaluator_parity_matrix.md](runtime_evaluator_parity_matrix.md)。Actual RawTurnPlanner 的 scope、parse、turn resolution、task patch、reducer、typed IR 和 LogicalPlan 直接观察；Pending 使用真实合同与 builder/seal/restore。Dataset 的 V2 executed-receipt adapter 为 MISSING，Dry Plan 仅在独立的原生 seam 上补充观察。没有把 TEST_ONLY_APPROXIMATION 作为完整 runtime-equivalent。

## 2–6. 单轮、历史、Pending、Dataset 与 NOT_RUN

原 100 条正式定义为 PUBLIC_DEV：58 TRUE_SINGLE_TURN、38 CONTEXT_DEPENDENT_TURN、2 HISTORICAL_RETURN、1 PENDING_RESPONSE、1 DATASET_FOLLOWUP。20 条 Transition 也已暴露，仍属于开发集：15 context-dependent、1 historical、1 pending、3 dataset。两个集合合计 58 真单轮、56 可执行历史场景、2 Pending 和4 Dataset 场景。另有24条 private validation（8单轮、16多轮）。Pending/Dataset 的描述型前置条件不算成已执行历史轮。

| 集合 | Case | Required turns | Executed turns | 有可评轴 Case | 全部已声明轴可评 Case | PASS | FAIL | NOT_RUN | BLOCKED |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| public_dev | 100 | 140 | 108 | 69 | 41 | 39 | 55 | 0 | 6 |
| transition | 20 | 39 | 27 | 8 | 7 | 6 | 11 | 0 | 3 |
| private_validation | 24 | 40 | 37 | 21 | 1 | 0 | 24 | 0 | 0 |

PASS 只表示已声明标签轴与所需运行门禁通过，不等于 Whole Semantic Plan 正确。FULL_PLAN 真值为0。失败历史导致后续轮未执行时，Case 保留失败；没有独立历史标签的轮不会自动 PASS。HTTP/timeout 不进入语义失败分母。BLOCKED 9 = 三条模型超时 + 三条前置证据缺口 + 三条 V2 Dataset 能力缺口。不能以 NOT_RUN=0 推导 Critical Gold 全部可评。

Case 门禁比值分别为39/94、6/17、0/24；已带可评分标签的 Turn 比值为39/70、6/9、0/21。它们是不同单位，不汇总为45/144“准确率”。PUBLIC_DEV 的条件 Wilson 区间见 JSON；这是一组相关、经过选择的开发案例，不构成独立总体抽样或 Cutover 置信保证。小验证集标 EVIDENCE_INSUFFICIENT。

## 7. Semantic Stage 观察率

| 阶段 | 观察到原调用/拒绝 | 产生阶段 Artifact |
|---|---:|---:|
| CurrentTurnParse | 169/172 | 167/172 |
| TurnResolution | 167/172 | 167/172 |
| TargetTask | 167/172 | 167/172 |
| Mention | 169/172 | 167/172 |
| Semantic Role | 153/172 | 153/172 |
| Candidate Retrieval | 167/172 | 167/172 |
| Canonical Binding | 153/172 | 153/172 |
| TaskPatch | 145/172 | 101/172 |
| TaskSemanticState | 101/172 | 101/172 |
| Time Normalization | 101/172 | 101/172 |
| Filter | 101/172 | 101/172 |
| Query Shape | 85/172 | 84/172 |
| SemanticQueryIR | 85/172 | 84/172 |
| Clarification Decision | 83/172 | 83/172 |
| Dry Plan | 0/172 | 0/172 |

分母172为实际执行轮，计划轮为219。观察到拒绝不同于正确输出。Native Pending 合同、恢复和答复/新话题通过测试；本次 live Pending 仍在下游 source/edit 处拒绝，不能宣称完整 live clarification coverage。

补充 Dry Plan 共83个已接受计划：46个原生规划成功，34个 lowering UNSUPPORTED，3个返回 PINNED_SQL_REQUIRED_JOIN_MISSING。没有执行 SQL；没有把这些独立 seam 的结果加入 Whole Plan Gold。后3个是 SQL/Catalog 依赖边界诊断，仍需因果归属证据，不在 Agent 加补偿。

## 8–10. Full Plan / Mutation / False Positive

FULL_PLAN_GOLD_COUNT=0，WHOLE_PLAN_PASS=0，WHOLE_PLAN_FAIL=0，因此 Whole Plan Accuracy 不可用。14/14 类错误注入被判 FAIL；7/7 类等价表示检查通过。新增74项 Harness 测试全部通过；最后100项相关 Harness / Raw Replay / Oracle 测试全部通过。评测器已修正 Mention/Role 分流、HTTP 分母、真实 Runtime 错误包装、Pin 结束所有权、标签泄漏入口、比较版本与中途版本漂移、缺失观察，以及“只有标签形式不同却被判静默错查”的误报。

## 11–14. Splits / Prompt Freeze / Overfit

PUBLIC_DEV=100，Transition PUBLIC_DEV=20，PRIVATE_VALIDATION=24，BLIND_HOLDOUT=24。新私有两集来自冻结目录事实和明确的 ADD/REMOVE 业务合同组合，不是模型答案标签，不宣称独立人工裁决或代表性业务覆盖。Holdout 原句未打开、未运行，hash/IDs hash/创建时间/label version 已固定。暴露并用于修复即必须 PROMOTED_TO_DEV。

Prompt、Schema、Catalog、Candidate 与 Evaluator 已冻结。生产本轮 Regex/关键词/业务词特判/Prompt规则增量均为0；现有直接 semantic Regex=0、Schema pattern=2、语言词33、业务词特判0、Prompt句子单元92（最初 raw baseline34）。时间模块依赖的25处既有 Regex调用与13个pattern单独记录。OVERFIT_RISK=YES，PROVEN_PROMPT_OVERFIT=NO；相同 Harness 下 Early/Current Prompt 横向实验尚未完成，Blind 也未解锁，不能凭本轮 Public/Private 差异证明 Prompt 过拟合。

## 15–16. V1 / V2 Common-set

COMMON_EVALUABLE_SET=0：V1 0/0，V2 0/0，无排名。V2-only 可完整观察现有标签轴为41/7/1，V1-only=0。现有 V1 报告手动串联 rule services，不等于正式 orchestrator；不能拿旧 V1 表和新 V2 结果比较。还缺真实 V1入口的冻结 semantic/ASL response adapter、相同独立标签和不伪造执行的历史状态。

## 17. Post-Harness First Divergence

| First divergence | Case count |
|---|---:|
| TASK_OPERATION | 21 |
| TURN_RESOLUTION | 15 |
| ENTITY_VALUE_GROUNDING | 14 |
| MENTION_BOUNDARY | 13 |
| SEMANTIC_QUERY_IR | 9 |
| CATALOG_GAP | 6 |
| QUERY_SHAPE | 6 |
| CANONICAL_BINDING | 3 |
| EXTERNAL_BLOCKER | 3 |
| FIXTURE_GAP | 3 |
| IMPLEMENTATION_GAP | 3 |
| TIME_NORMALIZATION | 2 |
| SEMANTIC_ROLE | 1 |

每个非PASS Case只有一个第一分歧，其他失败轴只记 downstream_effects；全144条 Case 的明细、case IDs、严重度与覆盖上限分别见 `post_harness_case_inventory.json` 和 `post_harness_first_divergence.json`。阶段或 reason-code 家族不自动等于一个独立 Bug。旧73/100、6/20及旧分布只保留 PRE-HARNESS BASELINE 身份。

## 18–20. 评测根因 / 生产根因候选 / Top 3

评测/前置证据缺口：V1完整冻结入口缺失；G81-076/G81-088缺所指历史任务；G81-091缺 Dataset provenance。运行能力缺口：S81-018..020的 V2 executed Dataset adapter，以及 RawTurnPlanner尚未集成的 Dry Plan seam。前者不得用假fixture隐藏，后者不得用独立调用假装已集成。完整标签不足单独记录，不能从当前系统输出自动生成真值。

生产语义侧有10个根因候选家族；尚不能证明是10个独立Bug。Top3按影响覆盖、查询/状态风险和通用合同适用性排序：

1. Current-turn Slot/Operation evidence 对齐：21条 primary Case。确认 SET/ADD/REPLACE/REMOVE/CLEAR 的声明和消费合同；不得放宽拒绝规则以提高通过数。
2. Active Follow-up / Historical Return 引用合同：15条 primary Case。区别上一轮修改与返回历史任务；关系标签不同但实际查询相同的案例单列，不夸大为已静默错查。
3. Entity-value字段选择、候选闭合和请求消费：14条 primary Case。区分城市/省等真实目录字段与数据证据，保持 Probe 上限和 Scope 不变。

以上数字是可能一次通用修复覆盖的上限，不保证一个补丁全部解决。其余 Mention Boundary13、IR9、Catalog relation6、QueryShape6、AliasBinding3、Temporal2、SemanticRole1 保留证据。其中 Catalog 错误可能来自选错 QueryShape，尚未证明目录本身有缺陷。所有本轮生产语义修改=0。

## Critical Safety / Replay / Provider

| Check | 违规 / 已观察 Case | Gate |
|---|---:|---|
| catalog_id_fabrication | 0/49 | PASS_ON_OBSERVED_AXES_ONLY |
| clear_resurrection | 0/0 | INCOMPLETE |
| pending_hijack | 0/0 | INCOMPLETE |
| remove_last_resurrection | 0/0 | INCOMPLETE |
| scope_expansion | 0/49 | PASS_ON_OBSERVED_AXES_ONLY |
| unsafe_truncated_ranking | 0/0 | INCOMPLETE |
| wrong_inheritance | 0/7 | INCOMPLETE |
| wrong_silent_auto_accept | 0/42 | PASS_ON_OBSERVED_AXES_ONLY |

范围及ID证明在已接受当前轮中未发现违规；Clear/REMOVE-last/Pending/Dataset的关键 live 路径覆盖不足，Safety 总门禁 INCOMPLETE。160/160既有 Critical regression 只作为组件回归证据，不能抵销未执行业务链路。

本轮340次模型请求仅用于固定模型的 Harness采集，3次超时；没有正式 Model Benchmark。172/172新 Recorded Turn 在严格断网下重现。77个历史原始capture去重后64兼容通过、13因旧上下文/Prompt/Schema不兼容而保留历史身份；旧10条Parser控制与Oracle干预回执另行版本化，不计新Gold准确率。Replay模型/SQL/Milvus/生产Redis/生产状态写入均为0。候选枚举与实时ranked Retrieval质量分开，Live Retrieval未评估。

as_of固定2026-09-09T09:00:00+08:00，Asia/Shanghai。temperature=0、retry=0；seed/top_p/max_tokens未发送或Provider支持未建立，不能宣称确定性。相同完整request body的既有重复样本及响应hash变化见 `model_call_and_stability_receipt.json`，不混作语义准确率。

## 21. 下一阶段与门禁

尚不能以“完整 Harness 已关闭”为前提进入 SEMANTIC PRODUCTION ROOT CAUSE CLOSURE，更不能开始正式 Model Benchmark。已证实的具体Root候选可供下一阶段审查；先补 V1 同分母入口和历史/Dataset前置证据，明确 Dataset/DryPlan运行能力边界，再用同一冻结评测器验证通用生产修复。Critical Root收敛 → Semantic Freeze Candidate → Blind Holdout → Hard Safety PASS 后，才是 Model Benchmark。当前没有 V2_SEMANTIC_CANDIDATE_BASELINE，也没有 READY_FOR_USER_APPROVAL。

Redis生产恢复及Catalog正式发布证据继续约束 Canary/Cutover，不是本次离线评测的全局前置。

工程验证：Agent3136 passed /27既有failed；Oagnet677/8；SQL Translator381/0；collection errors=0；old-pass→new-fail=0。本阶段分支为 `evaluation-harness-closure-20260909t083000z`，堆叠到PR #52，最终证据提交与Draft PR由Git manifest记录。原Oagnet Git HEAD/index、凭据、配置与V1路由保留。
