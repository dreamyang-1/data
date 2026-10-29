# Semantic Production Root Cause Closure — Round 4

**SEMANTIC_ROOT_CLOSURE_ROUND_4_COMPLETE — SCHEMA_GAP_CONFIRMED / MIXED_ROOTS**

本轮确认 Context Proposal 的结构缺口，建立并验证了有上限的候选曝光、独立关系/目标提案和硬约束校验的实验合同。**生产修复门禁尚未证明，只提交诊断代码、测试和证据。** 生产源码、生产 Prompt/Schema、公共 API/SSE、Reducer 和 V1 正式路由修改均为 0。没有把实验 B/C 直接接入生产，也没有开始下一 Round。

## 基线与版本

开始 HEAD 为 `466482b67703bf19214ef54c852c606ee14be109`，父 Draft PR [#57](https://github.com/dreamyang-1/data/pull/57)。Git 干净，1519 个 tracked file 按正确的 Agent/Oagnet/SQL 目录映射核对，无意外差异。开发目录 `E:/YouoAgent/DataAnalysis_Agent`，版本仓 `E:/yy`，独立分支 `semantic-context-proposal-round4-20260910t004000z`。

实验 Schema 独立提交；诊断与收尾证据为第二个提交。最终 HEAD 由 `git_commit_manifest.json` 的 introducing-commit 命令精确解析，远程发布回执在提交外保存，避免递归写入自身 SHA。开始时间为 2026-09-10 00:40 UTC，2 小时硬停止门禁仍适用。

## 当前真实数据流与结论

详见 [CURRENT_CONTEXT_PROPOSAL_DATAFLOW](current_context_proposal_dataflow.md)。当前第一阶段 LLM 只有当前句、turn ID、clock、slots，没有任务或 Pending 上下文；它输出 acts/reference/followup/shift 信号，没有独立 relation/target proposal。历史曝光由 `_task_context` 和 `semantic_task_schema` 根据前置 HISTORICAL/shift 判断控制；最终 relation 和 target 仍由 TurnResolver 推导。

**STRUCTURAL_CONTEXT_PROPOSAL_GAP_CONFIRMED**。Candidate Discovery 与 Relation Decision 在生产里尚未充分分离。V2 没有人工分数竞争或 missing_slots 关系判定，但固定字段优先级仍是 soft semantic policy，不能全部改称 hard constraint。原 15 条已有正确 target 的结论保持：15/15 是冻结基线，不是本轮新模型 15/15；本轮 B 只新覆盖其中 S81-002 一条，其他 Private 原句未被打开或送模型。

## 实验合同与模型上下文

`MAX_CONTEXT_TASK_CANDIDATES=4`：先 Active、Pending，再少量 recent tasks；不接收 parse/relation 作为曝光条件。冻结语料 172 个首次 Parse 的存储任务分布为 0:136、1:35、2:1；已有受控曝光场景为 3 个。4 是本轮小范围实验上限，不是经过校准的最优值。测试另覆盖 7 个任务仅曝光 4 个；没有新 Memory、数据库、向量索引或 Context Engine。

模型看结构化 task_id/version/status、指标/维度/实体标签、filters、time、已有 query shape、barriers 和 created/updated order。没有传完整聊天。当前 TaskVersion 没有 last user semantic delta；显式标为 unavailable，不从文本猜造。Task/Pending Scope 由当前 session restore 核验，模型不能创建或扩大 Scope。

复用首次 Recognition 调用，联合产生 CurrentTurnSemanticParse 和 `context_proposal`，不增加辅助 LLM 调用。relation 使用既有枚举；target 只能是候选 ID 或 NEW/null；歧义/未解析不选目标。确定性代码检查 membership、scope、存在性、状态/任务版本、relation-target 一致性、Pending 版本及原生答案可接受性。TaskPatch/Reducer、当前显式输入和 CLEAR/REMOVE barrier 保留。

实验 Native adapter 只将已验证的提案投影到四个旧 relation signal 字段，以穿过现有 bridge；它不改 mention、slot 声明、operation marker 或编辑值。适配仅在诊断 context manager 内生效，不是生产接入。这个边界和部署前仍需的合同接入在 [schema_contract.md](schema_contract.md) 中明确。

## Hard/Soft、Confidence 与 Trace

[LLM_RULE_ARBITRATION_MATRIX](llm_rule_arbitration_matrix.json) 保留当前源代码优先级，并单列实验职责。Scope/候选/版本/Pending 合法性可以拒绝提案；句子短、缺指标/时间、自包含词法判断及人工 confidence 不得覆盖合法实验提案。当前显式 delta 和 barrier 的验证继续在 native patch/reducer，Trace 没有谎称 relation validator 已完成这些检查。

[CONTEXT_CONFIDENCE_INVENTORY](context_confidence_inventory.json) 引用未改源码的完整 121 项父审计，单列 27 项直接 Legacy relation/structured/default 定义位置。TurnAdmission 的 0.90、0.99、0.96、0.97、0.95、0.94、0.93、0.92 是 heuristic assignment；模型采纳默认门槛 0.80、reconcile 阈值 0.75 未见真实校准。0.5 schema 下界另标 schema bound。它们不是概率；未改任何权重/阈值。V2 relation confidence 消费/加权竞争仍为 0。

生产统一 arbitration Trace **仍未集成**。实验已记录提案、候选、约束结果、最终决定、override direction/reason 和 context hash；不发布完整敏感 Prompt、CoT 或 raw task state。模型不可用/非法时 fail closed，不默认继承旧任务。

## 三臂对照与分母

9 条冻结公开 Transition + 3 条新公开对照；新对照复用真实已存在的冻结任务/Pending 前置，没有编造执行历史。A 的前 9 条为原始 actual-runtime 冻结回执，新增 3 条使用 unchanged first Parse + native relation/Pending seam；B 为独立 context-aware 模型提案 + hard validation；C 为既有 V1 rule relation 入口，不是完整 V1。都是固定每轮 BEFORE state 的诊断，不是新策略从首轮开始的完整 rollout。

| 指标 | A Current Hybrid | B Model Proposal + Hard | C Rule-only relation |
|---|---:|---:|---:|
| 尝试输入 | 12 | 12 | 12 |
| 有效关系诊断输出 | 12 | 11 | 12 |
| Schema/Runtime 拒绝 | 0 | 1 | 0 |
| TargetTask exact，有明确目标真值 | 11/11 | 11/11 | 10/11 |
| Relation semantic match，有效输出分母 | 11/12 | 11/11 | 10/12 |
| Follow-up recall，严格关系族 | 6/7 | 7/7 | 7/7 |
| New-task precision | 2/2 | 2/2 | 2/2 |
| Historical Return relation+target | 1/1 | 1/1 | 0/1 |

A 的 S81-002 是正确 active target 配 RETURN_TO_TOPIC 标签，不能夸大为错误继承。B 的歧义样本 R4L-011 被 Schema 拒绝；null target 不算“正确识别歧义”，不加入有效语义分母。三臂的完整系统优劣、总体生产准确率或概率校准 **NOT_PROVEN**。12 个经过选择且相关的开发输入不构成生产抽样。细节和每条结果见 [experiment_metrics.json](experiment_metrics.json)。

A 的新任务观察器最初漏走现有 span repair，自审后用同一个已记录输出按原生顺序重评；最终 A 恢复 NEW_TASK。修正只涉及诊断接法，未调用新模型、改生产或改期待。

## 受控状态与真实模型原生链

Context Critical Slice 仍 **8/8、18/18**。实验 B 在同一原生前置、受控 semantic draft 下的 next_state 与 A 逐轮完全一致，包含版本、barrier、历史 task 和 Pending 状态。另 5 个 Round3 act-only 控制在显式合法 ADD 提案下从拒绝变为接受，保留旧指标和当前 ADD；这是受控合同改善，不是 5 个真实模型 Bug 被修复。

首次 Proposal 采集后，将 11 个合法提案及同次当前 Parse 送入原生 RawTurnPlanner 诊断，并对其下游 Draft 使用真实固定模型：**10 ACCEPTED_DIAGNOSTIC_ONLY、1 REJECTED**；另外 1 条因提案拒绝未进链。接受不等于 Whole Plan PASS。

10 个接受输出的已标注 state axes 未发现错误；其中新独立订单任务没有继承旧指标/维度/filter/time，Pending 单选真实恢复到既有 task。10 个输出没有新增不必要追问。S81-014 的 time_relation 轴未由这个 observer 单独评分，其他未标注条件也不能被推算正确，因此不宣称完整 wrong-inheritance/live Safety 已关闭。

S81-016 的提案正确选择 NEW_TASK，原生下游因 `V2_SLOT_OPERATION_CONFLICT` 拒绝；其旧基线也拒绝于 source-value 请求消费。未形成 Pending hijack；没有把安全拒绝算作业务成功，也没有在本轮修改 Slot/Entity Value。既有接受 → 本次诊断拒绝为 0，但这只针对本次小切片。

## Schema 自审、调用与验证

12 次 B Proposal 使用实验 Schema v1。歧义响应给出 `status=AMBIGUOUS`、历史关系假设、target=null，Python validator 要求 unresolved relation 也为 null，因此拒绝。自审确认这些跨字段谓词未完整进入模型实际收到的 JSON Schema；这属于**实验 Schema export gap**，不能归咎为生产模型能力根因。

最终实验 v1.1 将相同现有谓词导出到 Schema；没有放松 validator、改标签或把 v1 拒绝重算为 PASS。v1.1 离线验证通过，**尚未重新进行 live 验证**，因此不能把所有提案合同门禁标 PASS。生产 Schema/Prompt 始终不变。实验只追加通用提案合同说明，没有业务词、Gold 原句或 case-specific semantic rule。

本轮真实模型请求 **26**：12 次 B 首次提案 + 3 次新增 A 首次 Parse + 11 次下游 Draft/既有 Probe。模型 qwen3.7-max，Thinking=false，temperature=0，retry=0，每请求 timeout=60s。Scope81/[205]、真实冻结 Catalog 和 live as_of=2026-09-09T09:00:00+08:00 固定；受控 CFCS 保持其既有 2026-09-08 时钟，二者不混算。不是 Model Benchmark。

首次 B 提案延迟中位数约 7.53s、范围 6.55–27.56s。没有同条件配对 baseline，**增量 latency UNKNOWN**；未固定 tokenizer，不把字符数当 token。提案复用首次调用，架构调用次数增量为 0，生产配置/调用次数变化为 0。没有确定性或成本改善保证。

专项验证 **322 个不同节点 PASS**：先 320 PASS，新增评测拒绝反例后模块 25 PASS，其中 23 重叠；新增测试共 25。Critical160/160、CFCS8/8，old-pass→new-fail=0，collection errors=0。父 Round1–3/Calibration/Harness verifier 通过。本轮未重跑 Agent 全量；3183 passed/27既有 failed 仅为 Round1 实际旧全量基线，没有机械推算新全量数字。Oagnet/SQL 未修改、未重跑。

原始记录和上下文留在忽略目录；Private Remaining=20，promotion=0，未打开剩余 Private 或 Blind。源 SQL、生产 Redis/业务状态写入为 0；本轮测试全为离线。没有 Benchmark、Shadow、Canary 或切流。

## Gate 与停止点

| Gate | 结论 |
|---|---|
| STRUCTURAL_CONTEXT_PROPOSAL_GAP | CONFIRMED |
| Controlled contract improvement | PASS_ON_CONTROLS，5 act-only 改善、18轮状态一致 |
| Production fix gate | NOT_PROVEN：合法歧义输出未闭合；最终实验 Schema 未 live 验证；原15簇新B覆盖1/15；完整当前 delta/继承和相似历史覆盖未齐 |
| CONTEXT_ATTACHMENT_CORE_READY | NOT_READY，生产独立 proposal/context/trace 尚未接入 |
| CONTEXT_FOLLOWUP_READY | NOT_READY，Core 与完整真实 Safety 覆盖尚缺 |
| SEMANTIC_FREEZE_CANDIDATE | BLOCKED，Critical Roots/Hard Safety 未收敛 |
| READY_FOR_USER_APPROVAL | NO |

下一 Root 优先级：1) 完成实验 relation/status/target Schema 导出的一致验证及明确的生产接入合同，补歧义与相似任务/原簇回归；2) 引用说明与当前显式增量声明边界；3) 既有 role/operation、QueryShape/IR 的后续分歧。本轮没有继续逐 Case 修改这些逻辑。

Catalog 发布和 Redis 生产恢复仍只约束相应 Canary/Cutover；不作为离线合同实验的全局前置。Certified Whole Plan/Dataset/DryPlan 边界沿用前阶段，未扩展审计。Schema 单独提交、其余诊断证据提交并推送 Draft PR 后停止；V1 保持正式路由。
