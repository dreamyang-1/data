# Semantic Production Root Cause Closure — Round 3

**SEMANTIC_ROOT_CLOSURE_ROUND_3_COMPLETE — MIXED_ROOTS**

原 TurnResolution 簇没有证明通用的错误任务附着缺陷。15 条均选中了预期既有任务；不能通过调整关系标签、降低 slot guard 或增加关键词来“修复”这个簇。本轮完成实际调用链、LLM/规则裁决、上下文曝光、冻结消融及单变量 Oracle 审计；生产代码、Prompt、Schema 和 V1 正式路由修改均为 **0**。这不表示 Context Core 或 V2 替换就绪。

## 基线与版本

开始 HEAD 为 `d01d305e23059d99d147aa4a5d2f1ff6022e02e1`，PR [#56](https://github.com/dreamyang-1/data/pull/56) 为 OPEN/DRAFT/未合并，origin HEAD 一致，Git 干净，1490 个 tracked file 与开发目录一致。没有本任务遗留脚本/测试进程；原有服务进程保留。开发目录 `E:/YouoAgent/DataAnalysis_Agent`，版本仓 `E:/yy`。

独立分支 `semantic-context-round3-20260909t141050z`，堆叠到 Round2；一个提交、一个 Draft PR。最终提交由 [git_commit_manifest.json](git_commit_manifest.json) 的 introducing-commit 命令解析，提交后核对远程 head/base，不递归写入自身 SHA。

## 模型与确定性代码的真实职责

完整 [CONTEXT_ATTACHMENT_DATAFLOW](context_attachment_dataflow.md) 和 [LLM_RULE_ARBITRATION_MATRIX](llm_rule_arbitration_matrix.json) 已建立，另有 [CSV](llm_rule_arbitration_matrix.csv)。

V2 自然语言关系信号来自 LLM 的 dialogue_act_candidates、reference_signals、followup_signals、topic_shift_signals。它不是一个直接由中文关键词驱动的 TurnResolver。但第一阶段模型只收到当前句、turn_id、clock、slots；没有 Active/Historical/Pending 上下文。后续模型能否看到历史候选，已经被这次无上下文 Parse 决定。第二阶段虽能选历史 handle，却不能独立输出最终 relation 或选择普通 active target。

最终 TurnResolver 采用固定字段优先级：topic shift → HISTORICAL → reference/followup/特定 REMOVE-CLEAR 一致性 → NEW。因此确有 **semantic policy veto / exposure gating**，不能把它们全部称为 Hard Constraint。常规 CONTINUE/MODIFY/CORRECT 等 act 单独出现时不够；控制实验显示其目标附着可以失效。五个受控例不等于五个真实模型 Bug，操作 act 也不能自动等同于 follow-up。

Scope、task/candidate 存在性、版本、当前证据、Catalog role 和 barrier 属于确定性约束。当前请求 Scope 在模型调用前验证。TaskPatch/Reducer 确定性地应用当前增量与允许继承，模型不生成完整最终状态。`missing_slots` 不进入 V2 TurnResolver，未证明 V2 `RELATION_READINESS_COUPLING_DEFECT`。RawTurnPlanner 的 shape、payload、catalog、当前证据检查位于选定 target 之后；系统拒绝不自动转换为“请补指标”。

## 上下文曝光与冲突观察

对原 172 个执行轮、340 次已记录模型请求提取了不含私有原句的 [context_exposure_audit.json](context_exposure_audit.json)。逐次记录存储任务数、active 数、offered current/history 数、Pending 是否存在及是否提供摘要、输入哈希与精确字符长度。

HISTORICAL 信号成立时会提供全部已恢复 tasks；否则只给预决策选中的 active task，移除可选 handle；NEW 时为空。没有小规模 recency cap，没有把 Pending summary 发给任一模型，也没有独立的上下文 relation proposal。三个任务的受控反污染审计证明曝光为 0/1/3；它证明代码边界，不证明增加候选后真实模型不会选错。Context-only token 数为 **UNKNOWN**：未固定该模型 tokenizer，不能把字符数冒充 token 或把整个 Prompt 的 usage 当任务摘要 token。

本轮 [arbitration_coverage.json](arbitration_coverage.json) 根据原生观察与冻结模型字段重建裁决链，并记录模型关系/目标、最终关系/目标、覆盖方向和原因。原生生产代码尚没有一条完整的 consolidated arbitration event；本轮没有将离线观察器伪称为生产 Trace 已完成。

| 原冻结执行轮分类 | n/N |
|---|---:|
| MODEL_DECISION_ACCEPTED：目标与模型证据一致 | 167/172 |
| MODEL_DECISION_REJECTED_BY_HARD_CONSTRAINT | 0/172 |
| MODEL_DECISION_VETOED_BY_SOFT_RULE：目标被改写 | 0/172 |
| MODEL_DECISION_REPLACED_BY_RULE / RULE_ONLY_DECISION | 各 0/172 |
| MODEL_UNAVAILABLE_FALLBACK：实际为 transport fail closed | 3/172 |
| MODEL_DECISION_NOT_REACHED | 2/172 |
| MODEL_UNRESOLVED | 0/172 |

这些是当前可观察字段的重建，**不是独立最终 LLM relation/target 输出的采纳率**。相同目标的 RETURN/FOLLOW 标签差异单列，不算软规则 Veto。原冻结语料未观察到目标被软规则覆盖，不证明这种结构性风险不存在。五个 act-only 控制与两个 Pending 字段冲突控制揭示了这种优先级限制。Pending 中的精确选项若遇到 NEW_TASK act、却没有 shift，仍会恢复旧 Pending；增加 shift 后新建任务。这个相同短回答本身有歧义，不能据此宣布真实用户 Pending Hijack。

## Confidence 审计

[confidence_inventory.json](confidence_inventory.json) 扫描整个 app Python AST，列出 121 个数值定义/使用位置，区分 schema bounds 与 heuristic/default。没有找到 confidence-bin accuracy、reliability calibration 等实际校准材料。

V2 关系路径没有人工 0.x 分数竞争，也没有读取 `model_self_reported_confidence` 决定关系；该字段存在于候选证据模型，但此路径无消费者。

V1 存在明确的旧设计：TurnAdmission 赋值 0.90/0.99/0.96/0.97/0.95/0.94/0.93/0.92；structured model 受默认 **0.80** 阈值限制；`reconcile_model_relation` 在 **0.75** 以下直接忽略模型，self-contained gate 可否决模型 follow-up，模型只可修正少数既有 relation 状态。另有 strong-rule fallback 和 rewrite 阈值。它们标 **HEURISTIC_SCORE / CONFIDENCE_NOT_CALIBRATED**，不能解释为概率。没有发现 `rule_score > model_score` 的直接竞争形式；阈值/分支 Veto 仍存在。V1 本轮冻结，没有为 V2 的问题重构 Legacy。

## 原 15 条分歧重分类

逐条匿名证据见 [context_case_inventory.json](context_case_inventory.json)。

| 当前分类 | 数量 | 结论 |
|---|---:|---|
| 引用/显式编辑声明冲突簇 | 12 | 选对 target，拒绝于 V2_EXPLICIT_SLOT_DROPPED；两个代表已做原句及因果审查，其余十条保留匿名 Artifact 同簇证据，不能声称每条词面根因已独立证明 |
| MODEL_ROLE_AND_OPERATION_CONFLICT | 1 | G81-082 将分组维度解释为 subject，原 Parse REMOVE/ADD 与 Draft REPLACE 冲突；关系标签纠正无效 |
| RELATION_LABEL_ONLY_MISMATCH | 2 | S81-002、PV81-005；目标及已声明状态轴正确，不以标签差异夸大静默错查 |

TargetTask mismatch **0/15**。13 条在发布 TaskPatch/State 前安全拒绝；另外两条已接受且已声明状态轴一致。此簇无 Pending，无完整新话题，相关风险 **0/0、无覆盖**。Wrong inheritance、wrong task patch、wrong final state 未在两条有输出的已声明轴上观察到，不把拒绝路径计成已验证正确状态。

`EXPLICIT_DELTA_DROPPED` 的 guard 报告为 12 条；两条经原句审查的案例中，真正 ADD/REMOVE 已构造，额外未消费的是“保留原有指标”的引用说明，**不是丢了用户要求新增/删除的指标**。实际 first dropping boundary 为 `_patch` 的 explicit_slot_mentions consumption 检查。上游误断言被下游安全拒绝；其他十条保留同结构候选分类。S81-002 已有健康表示：可以保留一个候选 MEASURE 引用 mention，同时不把它列为新编辑义务，未证明必须扩 Schema。

## Oracle 与消融

[oracle_evidence.json](oracle_evidence.json) 保存 4 次 A 和 2 次 C 冻结回放、D/F 控制及 B/E 不适用理由。

- A 仅改最终关系标签、保持 target：两个代表仍 SLOT_DROPPED，G81-082 仍 OPERATION_CONFLICT；S81-002 的 TaskPatch 和 next_state 完全相同。未发布状态的案例明确为 BOTH_NOT_PUBLISHED，不能称“最终状态通过”。
- C 仅纠正一个 Parse Artifact 内已审查的引用声明：ADD/REMOVE 两代表都诊断性接受。第二阶段模型输出完全不变；其输入因 Parse 改变而不同，已记录。没有把 Oracle 接受计作 Gold PASS，也没有将这个实验实现成自动删 slot 的生产规则。
- D 只切换 resolver seam：5 个受控 act-only 输入由当前路径拒绝变为正确 active-task ADD；Scope/Catalog/patch/state guards 保留。这是字段合同/优先级风险证据，原冻结 15 条不具有该因果链。
- F 仅改 Parse 的 topic_shift_signals：2 个原生 Pending 控制显示 ANSWER 与 NEW 的分支变化、旧 Pending 状态和不变的输入状态。短回答真实意图未裁决，不算真实 Pending Hijack 修复。
- B/E 没有错误 target 或已证实错误继承可供单变量恢复，标 NOT_APPLICABLE；没有编造 Oracle 成功。

三臂为 A 已记录模型语义证据 + 原生约束，B 现有 V1 纯规则**关系入口**，C 当前 V2 hybrid。三臂保留同一当前 canonical-edit Artifact，B 不是完整 V1 系统，A 不是一个不存在的独立 context-aware 模型。固定每轮 BEFORE state，**不是新策略从第一轮开始的完整 rollout**；改变上下文后也不重新生成 Draft。

| 受控指标 | A Model+Hard | B Rule-only relation | C Current hybrid |
|---|---:|---:|---:|
| Target exact，共同有模型关系证据的轮 | 9/9 | 6/9 | 9/9 |
| Target exact，全部尝试轮 | 9/18 | 15/18 | 18/18 |
| Follow-up recall | 7/7 | 5/7 | 7/7 |
| New-topic precision | 1/1 | 10/13 | 10/10 |
| Historical return 成功 | 1/1 | 0/1 | 1/1 |
| 末轮显式增量保留，已声明轴 | 6/6 | 4/6 | 6/6 |
| 未改动非空 slot 继承 precision / recall | 14/14、14/14 | 10/10、10/14 | 14/14、14/14 |
| 错误继承，已观察未改动 slots | 0/14 | 0/10 | 0/14 |
| 不必要追问 / Pending Hijack | 0/10、0/1 | 0/10、0/1 | 0/10、0/1 |
| CLEAR 复活 | 0/1 | 0/0，未到达 | 0/1 |

A 的另外 9 个初始控制轮没有显式模型关系命题，所以 abstain；不能把 9/18 当真实模型准确率。C 对受控完整序列仍为原生 8/8、18/18；对照复跑逐轮确认 C 输出相等。所选策略的失败可以表现为系统拒绝，并不必然表现为用户追问或错误状态。

冻结 Public 的可执行当前轮为70，Transition为9：Public 的 target 独立标签为0/0，不能补标签制造 exact-match 分母；三臂 attachment follow-up 为7/7，C 的严格 relation 标签为57/58。Transition 中能由真实历史回执映射的 target 为 A9/9、B8/9、C9/9；历史返回 A/C1/1、B0/1。未执行前置、模型超时、Dataset 等缺口继续保留。完整分母、预测数、接受/拒绝及局限见 [ablation_metrics.json](ablation_metrics.json)。

**当前 hybrid 在这组关系入口诊断上优于旧 rule-only；“完整 Hybrid 真正优于独立 Model+Hard 系统”仍 NOT_PROVEN。** 没有独立最终关系输出和对称上下文，无法从这次消融证明后者。原 Public39/55/0/6、Transition6/11/0/3、Private0/24/0/0 的正式 PASS/FAIL/NOT_RUN/BLOCKED 不改写。

## Private、验证与安全边界

先完成匿名 Artifact 聚类，再于 `2026-09-09T14:12:47.926191+00:00` 提升 PV81-002 和 PV81-003，各代表一个操作子簇，然后才查看原句。Private Original24，先前 Promoted2，本轮新增2，累计4，Remaining20；其余只作匿名结构/聚合观察。Blind 未打开、未运行，调用0。

最终冻结回放 **136/136**：Public105、Transition27、已提升代表4个执行轮；3条超时缺 typed output 继续排除。最终三臂记录408次，关系 Oracle4、声明 Oracle2；受控逐轮三臂54次。初期未固定规则时钟的 B 结果已被固定时钟重跑替代，不混入结果。完整 original capture hash、模型上下文、失败 reason 和已接受输出均核对；所有回放无真实模型、SQL、Milvus、生产 Redis 或生产状态写入。

专项共 **320 个不同测试节点通过**，包含 Critical160/160、Context Slice8/8、16项新增。先315项通过，补充完整句/新任务/Pending冲突对照后复跑本模块16项，其中12项重叠。old-pass→new-fail=0，collection errors=0。完整 Agent3183/27 是 Round1 的实际旧全量基线；本轮依据第53条未机械重跑全量，也未推算新的全量数字。Oagnet/SQL 未改未重跑。父阶段 Round1/Round2/Calibration/Harness verifier 均通过。

生产改动0，因此本轮执行离线分支；LIVE_CONTEXT_FOLLOWUP_SLICE **NOT_RUN_NO_PRODUCTION_FIX**。真实调用、Prompt/Schema/生产 Regex/业务词特判增量均0。没有用离线安全控制替代 live Safety，没有正式 Benchmark、Blind、Shadow、Canary 或切换 V1。

## Gate、后续优先级与停止点

**Generic Context Attachment production defect proven in original cluster = 0；production fixes = 0。** 已证明结构性 Context Exposure/Relation Proposal/Trace 合同缺口；没有证明原15条需要通用 TurnResolver 修补。不要把这个数字解释为 Context 系统无风险。

1. 先闭合 bounded context 输入、独立 relation/target proposal 与可观察 hard-validation 合同。候选曝光不应依赖已做出的关系结论；需覆盖普通追问、历史返回、相似任务和 Pending，并保留现有授权/版本/barrier约束。
2. 处理引用说明与当前显式增量的通用声明边界，覆盖上限12、已审代表2。不能根据具体词或未绑定就自动删除用户请求。
3. 保留 G81-082 的 role/operation 及已有 QueryShape、IR 根因证据；按 NEXT_DIVERGENCE 转交下一阶段，本轮没有继续修。

| Gate | 当前状态及剩余条件 |
|---|---|
| CONTEXT_ATTACHMENT_CORE_READY | NOT_READY：语义模型上下文、候选曝光、明确 proposal/裁决 Trace 合同尚未闭合 |
| CONTEXT_FOLLOWUP_READY | NOT_READY：还需 Core、真实 Raw-text 模型链路及足够 live Safety；受控8/8不足 |
| SEMANTIC_FREEZE_CANDIDATE | BLOCKED：主要 Critical Root 和 Hard Safety 未收敛；冻结 pins 不等于候选就绪 |
| BLIND_HOLDOUT | SEALED，未满足首次入口；不解封 |
| HARD_SAFETY_READY | INCOMPLETE：组件通过，Pending/CLEAR/cross-scope/Dataset 的完整 live 覆盖仍不足 |
| MODEL_BENCHMARK | NOT_STARTED：冻结候选、受门禁约束的 Blind 与 Hard Safety 先行 |
| PLAN_ONLY_SHADOW | NOT_STARTED：相应评测、能力覆盖、无副作用隔离证据未齐 |
| CANARY / CUTOVER | V1/V2 同分母对照、Shadow、生产 Catalog 发布与 Redis 恢复证据及最终用户批准仍缺 |

Certified Full Plan 仍16 BLOCKED、Whole Plan PASS0；Dataset/DryPlan 能力缺口不在本轮补。Redis/Catalog 生产证据只约束对应 Canary/Cutover，不重新变成 Offline Evaluation 全局前置。当前不是 READY_FOR_USER_APPROVAL。本轮提交及 Draft PR 后停止，等待下一阶段指令；V1 继续正式路由。
