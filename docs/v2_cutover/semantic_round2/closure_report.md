# Semantic Production Root Cause Closure — Round 2

**SEMANTIC_ROOT_CLOSURE_ROUND_2_COMPLETE**

**NO_HIGH_IMPACT_GENERIC_PROJECTION_CONTRACT_DEFECT_PROVEN**。原13条的错误投影声明已存在于原始模型 Parse，Normalization 没有新增角色或 marker。当前代码区分 role hypothesis 与 explicit slot/marker，前者不会自动升级成后者。不能安全地把“没有 projection binding”当作撤销用户显式请求的依据。因此本轮生产修改为0，13条仍 FAIL，结束 Projection 调查，下一 Round 优先 **TURN_RESOLUTION / CONTEXT FOLLOW-UP ROOT CLOSURE**，本轮不启动。

## 基线与真实调用链

开始核对 PR [#55](https://github.com/dreamyang-1/data/pull/55) 为 OPEN/DRAFT/未合并；本地与 origin HEAD 同为 `4d12cf6dd11239e6f2e76f91f43bbdeed0bc2c2b`。Git 干净，1471个 tracked file 与开发目录一致，没有遗留本任务测试/脚本。Round1、Calibration、Harness 的冻结证据保持通过。

唯一分支为 `semantic-projection-round2-20260909t132659z`，base 为 `semantic-operation-round1-20260909t122901z`。开发在 `E:/YouoAgent/DataAnalysis_Agent`，显式同步 `E:/yy`，一个提交、一个堆叠 Draft PR。最终 SHA 由 `git_commit_manifest.json` 中 introducing-commit 命令精确解析；提交后另行核对远程 HEAD 和 PR head/base，不在提交内部递归写自己的 SHA。

实际链路见 [projection_causal_chain.md](projection_causal_chain.md)：User Surface → 原始模型 Mention/候选角色 → **同一原始模型 Artifact 独立声明的** projection slot/marker → 只做 span/ref 校验的 Normalization → 按角色枚举候选 → Draft 选择合法 subject/metric binding、未选择 projection edit → `_patch` 在编译 TaskPatch 前拒绝未消费 marker。仅删 marker 时，下一条 explicit slot guard 继续拒绝。不是候选生成器把一个可能角色转成了显式请求。

三类展示语义在现有合同可分别表示：USER_EXPLICIT_PROJECTION 使用 projection slot/marker 与显式 ProjectionSpec；DERIVED_DEFAULT_DISPLAY 由当前 Catalog 主属性派生；PRESENTATION_OR_QUERY_SHAPE_CUE 不必创建 projection field mention。真正明确但暂未绑定的请求仍需保留 unresolved 或拒绝，不能静默改成默认展示。没有证明需要新增 Schema primitive；`SCHEMA_CONTRACT_CANDIDATE` 不是本轮必要修复。

## 13条分类与 Hypothesis Hardening

| 最终分类 | 数量 | Case IDs | 当前风险与 Cutover relevance |
|---|---:|---|---|
| MODEL_ROLE | 12 | G81-001/003/008/009/010/012/058/059/060/061、PV81-010/016 | SAFE_REJECT；BASE_TASK_BLOCKER |
| MENTION_BOUNDARY | 1 | G81-066 | SAFE_REJECT；BASE_TASK_BLOCKER |
| PROJECTION_CONTRACT / NORMALIZATION / QUERY_SHAPE / OTHER primary | 各0 | — | Query Shape 只列 NEXT_DIVERGENCE |

逐条 raw/normalized role、projection declaration、binding、shape、拒绝和 relevance 在 [projection_case_inventory.json](projection_case_inventory.json)。12条 Role 子类为 MODEL_OUTPUT_ROLE_DEFECT：模型把实体的可能投影角色或展示表达同时断言成了明确字段要求；没有证实 NORMALIZATION_DEFECT、SCHEMA_REPRESENTATION_DEFECT 或 BINDING_FEEDBACK_DEFECT。

**义务在 Binding 前存在：YES；Runtime 自动把候选角色硬化：NO；通用 HYPOTHESIS_HARDENING_DEFECT：NOT_PROVEN。** A 是模型可能角色，C 是模型对用户显式请求的断言；它们都来自概率输出，却由不同字段表达。B 是后来通过绑定验证的 canonical fact，D 是后来派生的目录默认展示。疑似混淆发生在模型同时填写这些字段的语义上。必须保留显式未绑定请求的约束，不能把前置义务的存在本身视为生产缺陷。

影响范围为 FIRST_TURN_ONLY：13条均第0轮安全拒绝；其中5条 history prefix 失败间接阻断后续执行，但没有证据显示该投影问题错误选择既有 TargetTask、发布 TaskPatch、改变继承或静默错误查询。因此其优先级低于已知 follow-up / explicit-slot 问题。SAFE_REJECT 不算修复；正式分数不变。

## Oracle A/B/C、正向与关键反例

| Oracle | 单一 Artifact 干预 | 结果 |
|---|---|---|
| A Classification | 纠正 Parse 的 projection role/classification，并保持同 Artifact 内 slot/marker 引用一致；裸展示 cue 不再作为字段 mention | 13条中3诊断性接受、6 CATALOG_RELATIONSHIP_REQUIRED、4 V2_QUERY_SHAPE_CONFLICT |
| B Obligation | 保持 roles 不变，仅撤销 Parse 的 projection 显式义务 | 复用 Round1 同一 capture、同一 Runtime 的已完成实验；结果同 A。未重复运行 Round1 |
| C Boundary | 仅 G81-066 将“市名单”边界改为“市”；角色、marker、Draft 不变 | 仍 V2_EXPLICIT_OPERATION_DROPPED；边界修改本身不能撤销投影断言 |

A/C 本轮实际运行14次离线 Oracle；B 的13条继承回执逐条记录来源 hash。原始 capture 只读，干预输出和完整诊断 trace 独立保存在 PRIVATE。Scope81/[205]、as_of、目录及 Candidate Snapshot 保持冻结；A 修改角色后按角色枚举的实际候选可变化，不能谎称该中间候选 Artifact 完全一致。第二阶段模型输出始终原样；Oracle 不计模型或 Gold PASS。见 [oracle_evidence.json](oracle_evidence.json)。

正向控制包括真实 RawTurnPlanner 的显式字段、默认实体列表、Grouped Aggregate/Ranking、正反向声明关系、替换 subject 后重绑默认字段等。当前32项 Catalog Plan 回归和1项 Grouping/Ranking 回归全部通过；另外 G81-015、G81-089 两个已有 SCALAR_AGGREGATE Recorded Capture 严格回放成功。它们不是本轮新 Gold PASS。没有使用失败后的 Oracle 接受冒充正向原始案例。

新增反例证明：仅在实体上附加 PROJECTION_FIELD 候选角色、没有显式 marker 时，默认显示正常，不会触发强制投影。真实用户要求不存在的字段时，Draft 遗漏该字段触发 V2_EXPLICIT_OPERATION_DROPPED，只有 slot 时触发 V2_EXPLICIT_SLOT_DROPPED，明确 unresolved 时触发 V2_RECOGNITION_UNRESOLVED。三者都不能被当成普通展示 cue。

进一步的**反事实负控制**显示，若无条件使用 B 撤销义务，真实未绑定字段会消失，系统只返回默认 projection。这是对拟议放松规则的否证，不是当前生产发生的 UNSAFE_ACCEPT。该规则没有投入生产。新增8项 Projection/Oracle 测试全部通过。

## Default Display、RELATION_LIST 和停止条件

复用 Round1 六实体的原生目录证明，没有扩大 Catalog 审计。`complete_catalog_defaults` 在 `_patch` 和 shape 检查之后调用 `default_projection`；实际使用 `is_main_attribute`，核对 owner、唯一 attribute binding、field_mapping 和主属性标志。多个不同主字段按既有排序规则组成 projection；没有主字段或元数据冲突继续拒绝。默认补全 provenance 为 CURRENT_REFERENCE_RESOLUTION / PINNED_CATALOG_MAIN_ATTRIBUTES，不是 CURRENT_EXPLICIT。

6条 `CATALOG_RELATIONSHIP_REQUIRED` 来自普通实体列表被 Draft 选择为 RELATION_LIST，没有用户关系要求或关系 edit；另4条 Parse/Draft shape 冲突。**DETAIL_ROWS 已是普通实体列表可用的正式 Payload/QueryShape**，正向回归证明默认实体列表可执行，故未证明 ENTITY_LIST_QUERY_SHAPE_GAP。没有新增 Payload、重写 LogicalPlan、修 Catalog Relation 或 SQL Translator。下一分歧明确后即停止该分支。

满足本轮防止过度调查的停止条件：raw model 声明错误已定位；10条下游 QueryShape/Relationship 已明确；原13未显示既有状态/上下文安全影响。没有依据再启动一轮 Projection 深挖。

## Context Follow-up Critical Slice 与验证

固定 **context-followup-critical-slice-v1 / PUBLIC_DEV**，8条、18轮均执行，PASS8/FAIL0/NOT_RUN0/BLOCKED0。复用 G81-068/070/071/072/073/075 等已确认操作语义和已有 Raw/Structured/Pending 回归，在本轮明确业务要求下组合成以下控制：

| Case | 场景 | 结果 |
|---|---|---|
| CFCS-01 | 去年上海销售额 → 那江苏呢 | 保留指标/时间，替换地区 |
| CFCS-02 | 去年上海销售额 → 再加订单笔数 | 保留旧指标/地区/时间 |
| CFCS-03 | 去年上海销售额和销售数量 → 不要销售数量 | 精确 REMOVE，保留销售额 |
| CFCS-04 | 去年上海销售额 → 不限地区 → 换今年 | CLEAR barrier 持续，地区不复活 |
| CFCS-05 | 上海和北京去年销售额 → 不要上海 | 仅剩北京 |
| CFCS-06 | 上海销售额 → 换北京和江苏 | 单项替换为两项，上海消失 |
| CFCS-07 | 上海任务 → 医院销售额新任务 → 返回上海任务 | 选择显式历史 TargetTask |
| CFCS-08 | 真实 Pending 问指标 → 江苏有哪些医院 | NEW_TASK，旧 Pending suspended，无旧指标继承 |

每轮记录真实 TurnResolver 输出、TaskPatch、structured edit trace、TaskSemanticState、TargetTask、Clarification Decision 和 Pending 状态，检查输入状态没有原地变异。结果在 [context_followup_critical_slice.json](context_followup_critical_slice.json)，后续 Production Semantic Fix 必须回归此切片。

**切片证据边界：受控 Recognition + 原生 Runtime + 既有合成目录。** 它验证状态合同，不是当前模型的8条真实业务准确率，也不认证江苏对应 city/province 的物理字段 Grounding。原有 fixture 的城市字段在这里仅作为地区成员操作载体；真正字段选择仍属于源值/语义评测。CFCS-07 显式提供上海前置任务，不回填 G81-076 缺失的历史，不重写旧 Gold。Blind 没有参与。这8条通过不能单独宣布 CONTEXT_FOLLOWUP_READY。

本轮专项共 **209 passed /0 failed**：193个既有节点与原基线比较均通过，16项新增通过；Critical160/160；old-pass→new-fail=0；collection errors=0。完善切片观察后另重跑8条通过。全量3183 passed /27既有失败仅作为上轮基线，**本轮没有重跑或推算新全量结果**。Oagnet/SQL 未修改、未重跑。见 [test_delta.json](test_delta.json)。

新测试开发时修正了测试自身的 UTC/本地时间断言、LIST 类型、时间 mention 边界、Pending alias candidate 子集；未修改生产或旧测试 expectation。Prompt、Schema、Semantic Regex、业务关键词、Evaluator、Validator 和生产 API/SSE/V1 路由增量均0；真实模型/源SQL/生产写入0。剩余22条 Private 不逐条读取调试，新增 promotion0，PV81-010/016继续作为已提升 Dev；Blind 完全封存。

## 下一 Root 与替代主线

排序见 [cutover_priority_and_gates.json](cutover_priority_and_gates.json)：1) TurnResolution / Explicit Slot Preservation，覆盖上限12，优先解决 FOLLOWUP_BLOCKER；2) TargetTask / Historical Return / Pending 的真实前置和安全覆盖；3) IR / Recognition Unresolved，覆盖上限6；4) 本轮 Projection 的13条 BASE_TASK_BLOCKER。这里只排序候选，未把潜在静默风险写成已证明生产 P0，未开始下一 Round。

| Gate | 尚缺 |
|---|---|
| CONTEXT_FOLLOWUP_READY | 真实 Recognition 的轮次/任务/slot 根因收敛与 live Safety；受控8条不足以关闭 |
| SEMANTIC_FREEZE_CANDIDATE | Critical Root 与 Hard Safety 收敛；现有冻结 pins 不等于候选就绪 |
| BLIND_HOLDOUT | 达到冻结候选入口条件后才首次运行，当前封存 |
| MODEL_BENCHMARK | Critical Root、冻结候选、受门禁约束的 Blind 和 Hard Safety；本轮禁止 |
| PLAN_ONLY_SHADOW | 相应评测、能力覆盖和无副作用隔离证据；本轮禁止 |
| Canary / Cutover | V1/V2 同分母对照、Shadow、生产目录发布/Redis恢复证据及最终用户批准 |

Certified Full Plan 保持16 BLOCKED、Whole Plan PASS0，Dataset/DryPlan 缺口不在本轮补。当前不是 READY_FOR_USER_APPROVAL。V1 保持正式路由，提交与 Draft PR 完成后停止，等待下一阶段指令。
