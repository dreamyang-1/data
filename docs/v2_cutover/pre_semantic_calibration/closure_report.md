# Pre-semantic Closure Calibration

**PRE_SEMANTIC_CLOSURE_CALIBRATION_COMPLETE**

本阶段完成十项校准工作，允许进入有证据约束的 `SEMANTIC_PRODUCTION_ROOT_CAUSE_CLOSURE`。这不表示完整 Runtime、V1/V2 对照或切流评测已通过。到此停止，本阶段生产语义、Prompt、Schema、Candidate Retrieval Strategy、API/SSE 和 V1 正式路由修改均为 0；没有 Benchmark、Shadow、Canary 或切流。

开始状态已重新核对：PR #53 为 OPEN / DRAFT / 未合并，开发目录 `E:/YouoAgent/DataAnalysis_Agent`，Git 仓库 `E:/yy`，HEAD `5aaf64a235ca1766ee5c72bb24997aba924c10e0`，origin 一致，Git 干净，1409 个 tracked file 无意外差异。独立分支为 `pre-semantic-calibration-20260909t111841z`。校准代码提交为 `b2478b5125f2b7d8ce463b0b42b389f1b3462367`；最终文档提交由 `git_commit_manifest.json` 定位。

## 1–4. 四类 Gate

| Gate | 状态 | 阻塞的阶段 | 证据与下一步 |
|---|---|---|---|
| EVALUATOR_CORE_READY | **PASS** | 无全局评测器阻塞 | 已观察 Runtime 的 parity、Case/Turn 分母、错误注入、等价表示、严格回放、版本/输入哈希、时钟、Provider 分离和 Split 管理已校准；使用当前冻结评测器继续调查已证实分歧。 |
| RUNTIME_CAPABILITY_COVERAGE_READY | **PARTIAL** | 相关能力及 Cutover | Pending、State、IR 可观察；Dataset 接入、主链 DryPlan 及部分 Safety 路径仍缺失。按能力关闭。 |
| V1_V2_COMPARISON_READY | **BLOCKED** | Cutover Comparison | 最小真实入口 Adapter 已建立，但缺少当前冻结 V1 HTTP/历史回执，Common Set 为 0。补精确依赖回执，不重构 V1。 |
| CUTOVER_EVALUATION_READY | **BLOCKED** | Benchmark / Shadow / Canary / Cutover 的相应门禁 | 能力覆盖、对照和 Hard Safety 尚未齐备；Blind 未解锁。 |

完整依据与 next_action 见 [gate_matrix.json](gate_matrix.json)。Core PASS 的范围是已实现且可观察的运行边界；没有把缺失的 Dataset/DryPlan 当成 parity，也没有要求它们成为 Turn/Mention/Role/Operation 评测的全局前置。

## 5. Private First Divergence

| 唯一 First Divergence | Private n/N | Case IDs |
|---|---:|---|
| TURN_RESOLUTION | 13/24 | PV81-002,003,005,006,008,011,012,014,015,017,018,020,023 |
| SEMANTIC_QUERY_IR | 6/24 | PV81-001,004,013,019,022,024 |
| TASK_OPERATION | 2/24 | PV81-010,016 |
| MENTION_BOUNDARY | 2/24 | PV81-009,021 |
| SEMANTIC_ROLE | 1/24 | PV81-007 |

其余阶段在此集合为 0/24。每个 Case 只有一个主分歧，后续错误保存在 `downstream_effects` / 原始 error 中。13 条轮次分歧里，12 条同时在后续报 `V2_EXPLICIT_SLOT_DROPPED`，1 条是已接受输出的关系标签差异；不能把它们全部叫作静默错查。明细和 Public 对照见 [divergence_shift.json](divergence_shift.json)。

## 6. Public / Private Distribution Shift

| 特征 | Public Dev | Private Validation |
|---|---:|---:|
| 真单轮 | 58/100 | 8/24 |
| 需要上下文，包括历史/Pending/Dataset | 42/100 | 16/24 |
| ADD | 3/100 | 8/24 |
| REMOVE | 3/100 | 8/24 |
| REPLACE | 4/100 | 0/24 |
| CLEAR | 3/100 | 0/24 |
| 历史返回 | 2/100 | 0/24 |
| Pending / Dataset | 1/100、1/100 | 0/24、0/24 |
| 任一轮出现时间条件的描述性标记 | 8/100 | 0/24 |
| 已标注指标涉及多个实体依赖 | 4/39 | 9/24 |
| 多指标没有共同的已声明实体依赖 | 0/39 | 7/24 |

Private 是指标汇总及 ADD/REMOVE 的程序化组合，8 条 ADD 的期待结果各含多个指标。Public 混合实体、字段、目录和多轮任务；其完整指标集合标签缺失，不能将 `multi_metric_expected=0/0` 当成不存在多指标。指标依赖依据冻结目录的 `source_dependency`，没有从失败输出反推标签。Private 的多轮比例、操作集中度和指标依赖宽度均不同；不能给出总体“更难”的单一排序。

Query Shape 的真值覆盖也不同：Public 只对部分 Case 标了 shape，Private 原标签没有 shape。Private 模板意图是指标汇总，但存在跨实体组合，不能未经独立标签就把 24 条全部认证为正确可执行的 SCALAR_AGGREGATE。Entity Value 的原始标签同样不能同分母比较：Public 的 selected mentions 没有完整覆盖地区值；按明确原句可确认 G81-068/069/071/075/094 至少包含当前实体/地区值，Private 的生成合同只组合指标，不注入实体值。此项是元数据复审，不改原 Gold 评分标签。

各特征、mention/operation 数量、query-shape 标签缺失及统计方法见 `*_difficulty_profile.json`、[distribution_shift.json](distribution_shift.json) 和 [catalog_dependency_difficulty.json](catalog_dependency_difficulty.json)。词面统计优先完整目录指标，避免将“已合作医院数”内部的“合作/医院”误计为关系查询或维度。所有比例是 curated related cases 的 n/N，不是生产总体准确率。

## 7–8. Prompt Generalization / Overfit

**PROMPT_COMPARISON_CONFOUNDED；PROMPT_OVERFIT_EVIDENCE = NOT_ESTABLISHED。OVERFIT_RISK 保留。**

Git 已恢复早期 `ab3c7fe` 的 Prompt，并完成输出协议对照。早期 Draft 没有现有的 filter/temporal/relationship/source-value 编辑协议；当前 Runtime 对已有 TimeSpec 的修改要求 component edit，拒绝旧式整体替换。直接比较两版完整 Public/Private 分数会同时测量协议迁移，不能只归因于 Prompt 内容。

存在共同的简单指标子集，不等于完整语料协议可比。此次没有给 Early Prompt 补当前失败规则，也没有修改 Current Prompt 或运行受混杂影响的准确率实验。不能由 Private 0/24 宣布 Prompt 过拟合，更不能宣称已证明 Prompt 不是主要瓶颈。证据见 [prompt_generalization_audit.json](prompt_generalization_audit.json)。

Private 仅用于特征聚合和第一分歧分析，未据此修改生产规则；本阶段 PROMOTED_TO_DEV = 0。24 条 Blind Holdout 原句未打开、未运行、未 Debug，沿用封存 Manifest。原始生成 Manifest 的 SEALED 状态与后续 Private access receipt 分开保存，不能误读为 Private 从未评测。

## 9–12. V1 Adapter / Common Set / 不可比较轴

新增最小 `V1OutcomeObserver`，调用真实 `DataAnalysisOrchestrator.handle`，观察最终请求、追问及 terminal response；不手动拼接 classifier/gate/parser，不要求 V1 创建 TaskPatch、TaskSemanticState 或 IR。受控测试确认 instrumentation on/off 的语义响应一致，包装器直接返回原结果或重新抛出原异常。

对 Public 的 58 条真单轮执行隔离入口诊断，全部因缺少冻结 HTTP 回执而 BLOCKED；42 条上下文/Pending/Dataset Case 没有匹配的原生 V1 历史前置，标 NOT_COMPARABLE。没有发出真实 HTTP/模型请求，没有用缺失依赖产生的降级结果冒充正式 V1 基线，也没有伪造已执行历史。

`COMMON_EVALUABLE_SET = 0`；V1 = 0/0，V2 = 0/0，无排名。V1-only = 0。V2 在其已声明 Runtime 轴上可评的 55 条（41 Public + 7 Transition + 1 Private + 6 新输入）是 **V2-only 观察覆盖**，不是 Common Semantic Outcome 的完整分母。

目前 task relation、clarification、metrics、dimensions、entity types/values、filters、time range/grain、query shape、scope、final semantic request、ASL semantics 都没有可用于同分母评分的 V1 冻结最终观察；各轴逐项保留 NOT_COMPARABLE。原生字段和 V2 canonical binding 不能靠 Adapter 猜测为同一个合同。详见 [v1_v2_comparison.json](v1_v2_comparison.json)。这一缺口继续阻塞 Cutover 对照，**不阻塞已由独立 Gold 证明的 V2 分歧调查和通用修复**。

## 13. 三条 Evidence Gap

检查了原 Gold builder、正式历史材料、已有 Recorded Replay/fixture，并搜索 261 个现有日志文件：没有找到这三条对应的补充历史或 Dataset 原始回执。

| Case | 结论 | 处理 |
|---|---|---|
| G81-076 | GOLD_EVIDENCE_GAP | 原历史只有江苏查询，缺少所引用的上海任务；不能虚构更早任务。 |
| G81-088 | GOLD_EVIDENCE_GAP | 原历史只有订单查询，缺少医院名单任务。 |
| G81-091 | GOLD_EVIDENCE_GAP | 缺少与 Case 绑定的 Dataset scope、snapshot、完整性和 ownership 证据。 |

三条仅局部阻塞，未生成假 Artifact，未改变原标签。恢复真实前置，或以新 Gold 版本进行裁决。搜索摘要见 [evidence_gap_review.json](evidence_gap_review.json)。

## 14–15. Certified Full Plan Gold

**FULL_PLAN_GOLD_COUNT = 16**，版本 `certified-full-semantic-plan-v1`。10 条复用 Public 输入及已有真实观察，6 条为新公开开发案例；因此全体唯一 Case 是 150，不能将 16 条再加到 144+6 上。

每条明确了 TargetTask、语义 TaskOperation、各角色 Binding、Filter、Time、Query Shape、完整 TaskSemanticState、IR、预期 DryPlan 和完整授权 Scope。标注依据仅为 CATALOG_PROVEN、BUSINESS_CONTRACT、DETERMINISTIC_RULE；没有以 V1/V2/LLM 输出生成期待。指标按正式目录名绑定，未裁决订单粒度、退货口径、科室唯一性或名称身份。

形状覆盖：SCALAR_AGGREGATE 14 条、GROUPED_AGGREGATE 2 条；覆盖单/多指标、单/多维度、ADD、REPLACE、REMOVE、CLEAR、普通 Follow-up 和完整历史返回。地区/实体值、时间、Entity List/Count、Ranking、Relationship 的完整标签仍未认证，逐项记录于 [full_plan_coverage_matrix.json](full_plan_coverage_matrix.json)，不能据此宣称全能力覆盖。

全部 16 条的已观察完整语义轴匹配；**Whole Plan = 0 PASS / 0 FAIL / 16 BLOCKED**，因为主链没有 DryPlan。2 条医院等级分组的 native DryPlan 期待为安全拒绝：冻结目录将医院等级绑定到医院，而所选指标绑定 sales_order；现有 direct-owner lowering 合同不能直接表达这一维度。此为当前边界的负向能力标签，保留合法业务分组语义，不把它定义成错误业务问题；未来接入跨实体维度时须升级标签版本。

空新任务的 SET/ADD/REPLACE 初始化只在 target=NEW、base version=0 时等价；单项 REMOVE 与一元素列表等价。多轮 ADD/REPLACE 继续区分，重复指标、角色错误、额外条件和 database/knowledge scope 差异均判 FAIL。这些校准由反例测试约束，不为提高业务分数放松判定。

新增 6 条共 10 轮使用固定当前模型执行，20 次模型请求；其原有已声明轴为 6/6 PASS，10/10 严格回放一致。它们是额外的公开合同控制样本，不是对 Private 原句调参，也不证明所有 ADD/历史返回问题已经解决。输入、标签与出处见 [certified_full_plan_gold.json](certified_full_plan_gold.json) 和 [full_plan_gold_manifest.json](full_plan_gold_manifest.json)。

## 16–18. Dataset / DryPlan / Safety

`DATASET_RUNTIME_INTEGRATION_GAP` 继续约束 Dataset Follow-up、截断排名及相关 Whole Plan/Shadow/Cutover；不阻塞独立 Turn、Mention、Role、Operation。没有制造 executed Dataset receipt。

`DRY_PLAN_RUNTIME_INTEGRATION_GAP` 继续独立跟踪。新增 10 个接受计划的 supplemental native seam：7 个规划成功、3 个因维度 direct owner 不匹配而 UNSUPPORTED。没有 SQL 执行，也没有计入 RawTurnPlanner 主链完成率。

| Safety check | 违规 / 已评 Case | 状态 |
|---|---:|---|
| CLEAR Resurrection | 0/0 | INCOMPLETE_NO_COVERAGE |
| REMOVE-last Resurrection | 0/0 | INCOMPLETE_NO_COVERAGE |
| Pending Hijack | 0/0 | INCOMPLETE_NO_COVERAGE |
| Wrong Inheritance | 0/7 | PASS_ON_OBSERVED_AXES；所需覆盖未齐 |
| Cross-scope Binding / Pending / Dataset | 各 0/0 | INCOMPLETE_NO_COVERAGE |
| Catalog ID Fabrication | 0/49 | PASS_ON_OBSERVED_AXES |
| Unsafe Truncated Ranking | 0/0 | INCOMPLETE_NO_COVERAGE；BLOCKED_BY_IMPLEMENTATION_GAP |
| Wrong Silent Auto Accept | 0/42 | PASS_ON_OBSERVED_AXES |
| Scope Expansion | 0/49 | PASS_ON_OBSERVED_AXES |

Hard Safety 总状态仍为 INCOMPLETE。新增 CLEAR Case 观察到清除本身，但没有其后再次追问的一轮，不能算 Resurrection coverage。Cross-scope Dataset 同样标 BLOCKED_BY_IMPLEMENTATION_GAP。组件回归不能抵销主链缺口。逐项 fixture/执行计划见 [critical_safety_coverage_matrix.json](critical_safety_coverage_matrix.json)。

## 19. Provider Stability

前阶段 340 请求：337 HTTP 200、3 timeout。本阶段新增 20 请求：20 HTTP 200、0 timeout、0 HTTP/schema error。合计 360 请求，357 HTTP 200、3 timeout；这不是 357 次语义 PASS。新请求同 hash 的响应差异和原 36 个重复请求组均单列在 [provider_stability.json](provider_stability.json)。

qwen3.7-max、Thinking=false、temperature=0、retry=0；seed/top_p/max_tokens 未设定或支持未认证。时钟保持 2026-09-09T09:00:00+08:00 / Asia/Shanghai。没有确定性或生产准确率保证。严格回放无外部调用，测试无真实模型调用，生产写入与 SQL 执行均为 0。

## 20–21. Post-calibration Root Causes / Top 3

原 144 条状态不改写：Public 39/55/0/6、Transition 6/11/0/3、Private 0/24/0/0，顺序均为 PASS/FAIL/NOT_RUN/BLOCKED。新增公开 6 条为 6/0/0/0；不合成一个总体准确率。原 99 条非 PASS 的 First Divergence 分布保持：Task Operation21、Turn Resolution15、Entity Value14、Mention Boundary13、IR9、Catalog6、Query Shape6、Binding3、External3、Fixture3、Implementation3、Time2、Role1。新增 6 条没有新的主分歧。

90 条语义侧 FAIL 按 stage、reason code、失败轴及输入族进一步拆为 **26 个调查簇**，仍不是 26 个独立 Bug。`MINIMUM_INDEPENDENT_ROOT_CAUSE_ESTIMATE = UNKNOWN`：当前没有因果干预证明它们相互独立。为报告漂亮强行填一个下界会制造虚假精度。每簇给出 MAXIMUM_CASE_COVERAGE 和共享合同边界。

| 下一阶段候选 | 覆盖上限 | 跨语料证据 | 优先调查 |
|---|---:|---|---|
| TASK_OPERATION / V2_EXPLICIT_OPERATION_DROPPED | 13 | Public11 + Private2 | 当前轮 operation marker 与 edit 消费的通用合同；保留拒绝保护。 |
| TURN_RESOLUTION / 后续 V2_EXPLICIT_SLOT_DROPPED | 12 | Private12 | 普通多轮引用与被丢弃 slot 的因果顺序；检查分布和标签影响，不直接假定需要改 Gate。 |
| SEMANTIC_QUERY_IR / V2_RECOGNITION_UNRESOLVED | 6 | Public1 + Private5 | 区分真实用户缺信息、跨实体指标依赖、模型未给出语义和 Runtime 能力缺口。 |

排序显式使用 severity × coverage × potential silent risk × cross-corpus generality。没有把“潜在风险”写成已发生的静默错查；Private-only 的第二簇仍需检查 DEV_OVERFIT_RISK。Entity Value 的 14 条缺少 Private 证据，在本轮跨语料排序下后移，不代表问题消失。Full Plan 新控制样本证明某些相同操作已有成功路径，修复必须继续缩小到共同 invariant。详见 [post_calibration_root_clusters.json](post_calibration_root_clusters.json)。

## 22. 下一阶段与停止点

**SEMANTIC_PRODUCTION_ROOT_CAUSE_CLOSURE_READY**，范围限于稳定证据支持的语义分歧调查及通过业务合同自审的通用修复。原因：Evaluator Core 已校准，Public/Private 差异已明确，无本轮生产调参污染，已有最小 Certified Full Plan Slice，关键 Safety 与 Runtime 风险已分开登记。

每项修复前仍须确认 first divergence、独立业务期待和正反例；不得将 reason-code 簇直接当一个 Bug，不得用具体城市、指标词、Case ID 或 Gold 原句打生产补丁。本轮到此停止，没有执行下一阶段修复。Benchmark 仍需主要 Critical Root 收敛 → Semantic Freeze Candidate → 首次 Blind Holdout → Hard Safety PASS。

Redis 生产恢复、目录发布继续约束 Canary/Cutover；V1 保持正式路由。当前不处于 READY_FOR_USER_APPROVAL，不申请或执行 V2 替换。

工程验证：Agent **3160 passed / 27 既有 failed**，新增 24 项校准测试；old-pass → new-fail=0，collection errors=0。既有 Critical 集合 160/160 与追问 reason trace 89/89 保持通过。Oagnet/SQL 源码未改，本阶段复用其已验证 677/8、381/0 基线，不声称重新运行。模型、日志、凭据、Private captures 与 Holdout 均不进入 Git；正式证据、回滚和哈希清单位于本目录。
