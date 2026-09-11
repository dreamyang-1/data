# Real-Business Failure Closure Round 1 — Remaining Failures

正式结果仍有 32/37 个 V2 失败 Case；另外 13/50 为原冻结定义下的 NOT_COMPARABLE，不被改成失败或通过。以下 `RB50-nn` 是冻结 Core 50 的顺序编号，完整原始文本和评价见未修改的 `core_50_result.jsonl`。

## 32 个剩余 Case

| 当前最早可见结果 | 数量 | Case |
|---|---:|---|
| `V2_CONTEXT_UNRESOLVED` | 11 | RB50-17, 18, 19, 20, 22, 24, 26, 28, 30, 31, 38 |
| 已产出 PLAN，但与期待任务/上下文不一致 | 3 | RB50-13, 14, 21 |
| `V2_BINDING_OUTSIDE_EDIT_EVIDENCE` | 3 | RB50-05, 32, 36 |
| `V2_SOURCE_VALUE_REQUEST_NOT_APPLIED` | 3 | RB50-25, 27, 29 |
| `V2_EXPLICIT_TIME_RANGE_EVIDENCE_REQUIRED` | 2 | RB50-01, 15 |
| `V2_RECOGNITION_UNRESOLVED` | 2 | RB50-16, 40 |
| `V2_SLOT_OPERATION_CONFLICT` | 2 | RB50-11, 44 |
| `V2_SOURCE_VALUE_CURRENT_MENTION_REQUIRED` | 2 | RB50-10, 37 |
| `CONTRACT_OR_HARNESS_FAILURE` | 1 | RB50-07 |
| `FROZEN_SOURCE_VALUE_OBSERVATION_REQUIRED` | 1 | RB50-12 |
| `V2_CONFLICTING_AMBIGUITY_SLOT` | 1 | RB50-43 |
| `V2_PAYLOAD_WOULD_DROP_SEMANTICS` | 1 | RB50-23 |

13 条 NOT_COMPARABLE 保持：RB50-03, 08, 09, 33, 34, 35, 41, 42, 45, 47, 48, 49, 50。当前 5 条 V2 PASS 保持：RB50-02, 04, 06, 39, 46。

## 指令要求的唯一失败分类

每个原失败 Case 只出现在一个分类中：

| 分类 | 数量 | Case |
|---|---:|---|
| `FIXED_BY_SOURCE_VALUE` | 0 | — |
| `FIXED_BY_TASK_PUBLICATION` | 0 | — |
| `FIXED_BY_TASK_RESOLUTION` | 0 | — |
| `FIXED_BY_MODEL_NORMALIZATION` | 0 | — |
| `CASCADE_RECOVERED` | 0 | — |
| `STILL_FAIL_SOURCE_VALUE` | 6 | RB50-10, 12, 25, 27, 29, 37 |
| `STILL_FAIL_CONTEXT` | 14 | RB50-13, 14, 17, 18, 19, 20, 21, 22, 24, 26, 28, 30, 31, 38 |
| `STILL_FAIL_MODEL` | 8 | RB50-01, 07, 11, 15, 16, 40, 43, 44 |
| `STILL_FAIL_CANONICAL` | 4 | RB50-05, 23, 32, 36 |
| `EVALUATION_BLOCKED` | 13 | RB50-03, 08, 09, 33, 34, 35, 41, 42, 45, 47, 48, 49, 50 |

`EVALUATION_BLOCKED` 是冻结 Benchmark 中原本的 NOT_COMPARABLE 集合，不进入 32 个失败的分母。`STILL_FAIL_CONTEXT` 包含 11 个 unresolved downstream Case 和 3 个已产出但任务/上下文不符的 PLAN；它不等于 14 个独立 Task Resolver Bug。

## 根因簇与优先级

### P0 — 模型 role 与后续 Source Value 使用不一致

部分 CurrentTurn 产物只为真实对象声明 `SUBJECT_ENTITY`，SemanticEdits 却创建需要当前 `FILTER_VALUE` 证据的 Source Value request。Guard 拒绝是正确的；不能把任意 subject 自动变成 filter。需要从同一冻结模型输入证明通用的跨阶段 role/edit 一致性合同，或在模型 Schema 中表达该约束，并保留不存在 value intent 的反例。

影响：RB50-10、37 是直接可见样本，其他 source request family 也可能受影响。风险是若错误放宽会把实体主题静默变成过滤条件。

### P0/P1 — Source Value 观察与请求消费仍未闭合

`REQUEST_NOT_APPLIED`、frozen observation、current mention 和 payload drop 仍分布在多个 Case。Round 1 repair 只处理可由当前显式 ADD 唯一证明的结构缺口。需要区分：

- 当前请求是否有真实、冻结、Scope-bound 的 candidate/source observation；
- 请求选中的字段是否与 edit 的 field/value role 一致；
- 是否已有完整 Filter Boolean structure；
- 失败是否只是评测 fixture 没保存 source observation。

没有这些证据时继续 fail closed，不能 fuzzy 绑定或静默删 mention。

### P1 — 首任务未发布造成的上下文级联

11 个 `V2_CONTEXT_UNRESOLVED` 是当前可见 downstream effect。正式重跑的首任务发布仍为 2/30，因此没有证据把这些失败重新分类为 `TRUE_TASK_RESOLUTION_ERROR`。下一轮应先恢复有合法前置任务的对照，再判断 relation/target 是否仍错；不得让 resolver 猜测不存在的历史任务。

### P1 — Binding / Ranking / Query Shape 结构

RB50-32 的诊断表明 metric binding 同时用于 metrics 和 `rank_by`，但 ranking edit evidence 只覆盖 limit；补齐 binding evidence 后又暴露缺少 `group_by`。这证明后续是 Query Shape/IR 结构问题，不是简单 handle mismatch。实验已撤回。RB50-05、36 仍需分别确定同样的第一分歧，不能用一个 ranking 补丁覆盖整个 family。

### P1 — Operation、Time 与 Recognition 合同

剩余 `SLOT_OPERATION_CONFLICT`、time evidence、recognition unresolved 和 ambiguity Case 需要逐一先确认原始模型语义是否正确。只有格式可由原句和当前显式 evidence 唯一校验时才允许 deterministic normalization；不能补原句不存在的业务语义，也不能通过调整人工 confidence 或关键词使 Case 通过。

### P2 — Evaluator / Fixture 边界

RB50-07 的 harness/contract 失败和 RB50-12 的 frozen observation 缺失需要先补评测证据，不应修改生产 Semantic Logic。正式 evaluator 当前含冻结 case verdict mapping；本轮保持不变。后续若改为通用 evaluator，必须版本化且同时重评 baseline/candidate，不能回写本轮分数。

## 本轮明确 Deferred

- Dataset 排名指代与截断结果全局排名
- typed Pending 无真实候选的数字回答
- ASL、SQL、Oagnet 与执行正确率
- Redis schema、Session 架构、新 Runtime、Catalog 平台
- Benchmark 扩容与复杂分析能力

## 下一最短路径

1. 先关闭 CurrentTurn role 与 SemanticEdits source-request 的通用一致性合同，使用有/无 Filter Value 意图的正反例。
2. 为现有失败保存 Scope-bound Source Value observation，分离生产缺陷与 fixture gap；不执行 SQL。
3. 仅对成功发布真实前置 Task 后仍失败的追问审计 relation/target，统计 `UPSTREAM_RECOVERY` 与 `TRUE_TASK_RESOLUTION_ERROR`。

当前最高阻塞是**真实对象在模型两个阶段中的 role/edit 语义不一致，导致严格当前证据链无法建立**。本轮不继续修改该根因。
