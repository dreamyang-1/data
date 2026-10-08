# Targeted First Divergence Report — Round 2.1

**TARGETED_FIRST_DIVERGENCE_REPORT_COMPLETE**

本报告只使用已经落盘的 6 个定向诊断 Case 和 12 次模型响应。恢复后新增模型调用、重试、SQL、Benchmark、V1 运行和生产写入均为 0；生产代码、Prompt、评分规则、Core50、8088、Oagnet 和 SQL Translator 均未修改。

## ROUND2_1_RECOVERY_STATUS

- 分支：`v2-role-edit-semantic-alignment-r2-20260911T151515Z`
- 基线 HEAD：`981841863d563137de6bcce2de574aabec779f84`
- 恢复时 Git 工作树：干净
- 完整 Case capture：6/6，分别为 RB50-32、RB50-36、RB50-25、RB50-27、RB50-10、RB50-37
- 完整阶段 exchange：12/12；CurrentTurn 6/6，SemanticEdits 6/6
- HTTP 结果：12/12 为 200；retry=0；前置轮调用=0；第三模型阶段=0
- 已生成 PRIVATE 证据：`case_receipts.jsonl`、6 份 case receipt、12 份 raw exchange、`case_analysis.json`、`validation_receipt.json`、`diagnostic_manifest.json`
- 恢复时已有公开报告 `ROLE_EDIT_FIRST_DIVERGENCE_REPORT.md`，内容来自定向调用前的证据审计，未纳入本次 6 个完整 capture，不能作为本轮最终结论
- 恢复时缺少的报告：本文件；现已完成
- 是否需要新模型调用：**NO；0 次**

PRIVATE 校验复算了所有输入、动态 Schema、raw response、consumed parse/draft 和原始受保护文件的哈希。1693 个受保护文件无变化；12 个 exchange 的通用输出模型校验均通过。动态 Schema 校验结果为 CurrentTurn 6/6 通过，SemanticEdits 3/6 通过、3/6 失败。

## 证据边界

这 6 组响应是 `NEW_TARGETED_DIAGNOSTIC`，不是 Round 1 正式 Benchmark 原调用正文。报告可以证明这些新诊断调用中的因果链、共享机制和代码边界；不能把它们改记为原正式 Case 的模型响应，也不能据此更新 5/37、2/30 或任何 Core50 分数。RB50-10 与 RB50-37 的结论均由各自完整的同次调用 CurrentTurn、SemanticEdits 请求、动态 Schema、raw response、consumed draft 和 Runtime 事件组成。

所有录制历史前置轮在原正式运行中都没有发布成功 state/plan；本次定向诊断没有重跑前置轮，`before.state/pending/plans` 为空。因此 RB50-25、RB50-27 的证据只证明当前目标轮在该真实空状态输入下的 pipeline 行为，不能证明有已发布 Task 时的 REPLACE/FOLLOW_UP 行为。

## 六个 Case 的第一分歧

| Case | CurrentTurn / SemanticEdits 动态 Schema | 最早分歧 | 后续最终拒绝 | 根因 | 置信度 |
|---|---|---|---|---|---|
| RB50-32 | PASS / PASS | ranking edit 只以 `m2`（前5）为 evidence，却引用 `m1` 的销售额 binding | `_patch:662`，`V2_BINDING_OUTSIDE_EDIT_EVIDENCE` | `EDIT_EVIDENCE_MISSING` | `PROVEN_FOR_THIS_NEW_DIAGNOSTIC_TRACE` |
| RB50-36 | PASS / PASS | Ranking Draft 没有生成 payload 合同要求的 dimensions/group_by | `materialize_payload:228`，RankingPayload `group_by` 为空 | `QUERY_SHAPE_STRUCTURE_MISSING` | `PROVEN_FOR_THIS_NEW_DIAGNOSTIC_TRACE` |
| RB50-25 | PASS / FAIL | raw SemanticEdits 响应违反已下发的 source-request `maxItems=0`，并生成未被任何 filter operand 消费的请求 | `resolve_requests:49`，`V2_SOURCE_VALUE_REQUEST_NOT_APPLIED` | `MODEL_SCHEMA_VIOLATION`；伴随 `DYNAMIC_SCHEMA_ENFORCEMENT_GAP` | `PROVEN_FOR_THIS_NEW_DIAGNOSTIC_TRACE` |
| RB50-27 | PASS / PASS | METADATA payload 已在 targets 保存 subject，但 semantic coverage 检查未承认该映射 | `_check_semantic_coverage:452`，`V2_PAYLOAD_WOULD_DROP_SEMANTICS` | `PAYLOAD_COVERAGE_MAPPING_MISMATCH` | `PROVEN_FOR_THIS_NEW_DIAGNOSTIC_TRACE` |
| RB50-10 | PASS / FAIL | raw SemanticEdits 响应违反已下发的 source-request `maxItems=0` | `resolve_requests:53`，`V2_SOURCE_VALUE_CURRENT_MENTION_REQUIRED` | `MODEL_SCHEMA_VIOLATION`；伴随 `DYNAMIC_SCHEMA_ENFORCEMENT_GAP` 和 `DRAFT_ROLE_ESCALATION` | `PROVEN_FOR_THIS_NEW_DIAGNOSTIC_TRACE` |
| RB50-37 | PASS / FAIL | raw SemanticEdits 响应违反已下发的 source-request `maxItems=0` | `resolve_requests:53`，`V2_SOURCE_VALUE_CURRENT_MENTION_REQUIRED` | `MODEL_SCHEMA_VIOLATION`；伴随 `DYNAMIC_SCHEMA_ENFORCEMENT_GAP` 和 `DRAFT_ROLE_ESCALATION` | `PROVEN_FOR_THIS_NEW_DIAGNOSTIC_TRACE` |

相同 error family 不是相同根因：RB50-32 与 RB50-36 原来同为 binding family，但本次第一分歧不同；RB50-25 与 RB50-27 原来同为 request-consumption family，本次也证明为不同根因。

## RB50-32

**原问题**：含税销售额排名前5的经销商。

**CURRENT TURN RESULT**

- `m1`：`含税销售额`，span `[0,5)`，roles=`[MEASURE]`，explicit=true
- `m2`：`前5`，span `[7,9)`，roles=`[LIMIT]`，explicit=true
- `m3`：`经销商`，span `[10,13)`，roles=`[SUBJECT_ENTITY, GROUP_BY]`，explicit=true
- markers：metrics SET←m1，ranking_spec SET←m2，subject SET←m3
- query shape：`RANKING`

**SEMANTIC EDIT RESULT**

- subject SET，经销商 binding，evidence=`[m3]`
- metrics SET，含税销售总额 binding，evidence=`[m1]`
- ranking_spec SET，rank_by 再次引用含税销售总额 binding，但 evidence 只有 `[m2]`

**DYNAMIC SCHEMA / RAW MODEL RESPONSE / SCHEMA VALIDATION**

- CurrentTurn raw hash `d643c1d5ece9316411a444e30ea15f743b2e83dd36b7dcef36d5951ba8e3fde9`，动态 Schema PASS
- SemanticEdits raw hash `513d0f0fddf8cab230ce67f9218fdffb8588c2f864fd461bc66e9cf243bc281f`，动态 Schema PASS
- Schema 没有表达“ranking 的 binding mention 必须也出现在该 edit 的 evidence”这一跨字段约束

**CONSUMED PARSE / DRAFT**：消费对象与 raw response 解析结果一致；span 全部有效，没有 Runtime repair 改写 Draft。

**FINAL GUARD**：`app/semantic_v2/recognition.py::_patch:662` 正确拒绝 `V2_BINDING_OUTSIDE_EDIT_EVIDENCE`。

**FIRST DIVERGENCE**：`SEMANTIC_EDITS_RAW_RANKING_EVIDENCE_OMITS_REFERENCED_METRIC_MENTION`。

**ROOT CAUSE**：`EDIT_EVIDENCE_MISSING`；动态 Schema 对跨字段 evidence coverage 的表达缺口是次级观察。该 Case 与 RB50-10/37 不共享根因。

## RB50-36

**原问题**：含税销售额排名前5的产品。

**CURRENT TURN RESULT**

- `m-turn-0-metric`：含税销售额，roles=`[MEASURE]`
- `m-turn-0-limit`：前5，roles=`[LIMIT]`
- `m-turn-0-subject`：产品，roles=`[SUBJECT_ENTITY, GROUP_BY]`
- markers 分别声明 metrics、ranking_spec、subject SET；query shape=`RANKING`

**SEMANTIC EDIT RESULT**

- subject、metrics、ranking_spec 的 binding/evidence 均正确覆盖
- Draft 没有 dimensions edit；reduced state 的 dimensions 为空

**DYNAMIC SCHEMA / RAW MODEL RESPONSE / SCHEMA VALIDATION**

- CurrentTurn raw hash `bf09dee0e0b165386215dc74e330238d380e5168d046d0c75f08b116b6aedc4d`，PASS
- SemanticEdits raw hash `c9ab9569992d955e59b7f6be75690951589f6fa796dcd375a081590ca0d6f13e`，PASS
- 当前动态 Schema 允许这个结构，但 `RankingPayload` 要求非空 group_by

**CONSUMED PARSE / DRAFT**：与 raw response 解析结果一致，未被 repair 改写。

**FINAL GUARD**：`app/semantic_v2/recognition.py::materialize_payload:228` 的 `RankingPayload` 校验拒绝空 `group_by`。

**FIRST DIVERGENCE**：`RANKING_DRAFT_HAS_NO_DIMENSIONS_REQUIRED_BY_PAYLOAD_CONTRACT`。

**ROOT CAUSE**：`QUERY_SHAPE_STRUCTURE_MISSING`，次级为 Schema generation gap。不是 Role/Edit alignment 根因。

## RB50-25

**原问题**：那一次性使用静脉留置针呢？

**CURRENT TURN RESULT**

- `m-turn-2-subject`：一次性使用静脉留置针，span `[1,11)`，roles=`[SUBJECT_ENTITY]`，explicit=true
- explicit slot：subject；query shape=`METADATA_LOOKUP`

**SEMANTIC EDIT RESULT**

- subject SET 为 product entity，evidence=`[m-turn-2-subject]`
- 额外生成 `req-product-name-lookup`；field handle 实际仍是同一 mention 的 `SUBJECT_ENTITY / ENTITY / product`
- 没有 filter edit 或任何 `value_request_id` consumer，请求为 orphan

**DYNAMIC SCHEMA / RAW MODEL RESPONSE / SCHEMA VALIDATION**

- CurrentTurn raw hash `e73a9d7d2a86d2aa511a7a33ce25e7aa2d3d60ae78d8a1256a4f104fdcef36d0`，PASS
- SemanticEdits raw hash `92a091f36621ea2f3db23bd5a109a734b9035e6c1e51f09c9b7175c1f6a885c1`
- 下发 Schema：`source_value_requests.maxItems=0`，`field_binding_handles.maxItems=0`
- raw response 各生成 1 项，并同时违反 conditional `anyOf`；动态 Schema FAIL
- 静态 `SemanticTaskDraft` 校验 PASS，说明入口没有执行本次动态 Schema

**CONSUMED PARSE / DRAFT**：消费 Draft 与 raw response 解析结果相同；违规内容未在入口拒绝或删除。

**FINAL GUARD**：`app/semantic_v2/source_value_recognition.py::resolve_requests:49` 发现 request 集合与 consumer 集合不一致，拒绝 `V2_SOURCE_VALUE_REQUEST_NOT_APPLIED`。

**FIRST DIVERGENCE**：`SEMANTIC_EDITS_RAW_RESPONSE_VIOLATES_ISSUED_DYNAMIC_SCHEMA`。

**ROOT CAUSE**：`MODEL_SCHEMA_VIOLATION`；`DYNAMIC_SCHEMA_ENFORCEMENT_GAP`、`DRAFT_ROLE_ESCALATION` 和 `SOURCE_REQUEST_ORPHAN` 为后续机制/效果。

## RB50-27

**原问题**：那人工心肺机系统呢？

**CURRENT TURN RESULT**

- `turn-2-m1`：人工心肺机系统，span `[1,8)`，roles=`[SUBJECT_ENTITY]`，explicit=true
- explicit slot：subject；query shape=`METADATA_LOOKUP`

**SEMANTIC EDIT RESULT**

- subject SET 为 product entity，evidence=`[turn-2-m1]`
- 没有 Source Value request、filter edit 或未授权 role
- materialized METADATA payload 的 `targets` 已包含该 subject

**DYNAMIC SCHEMA / RAW MODEL RESPONSE / SCHEMA VALIDATION**

- CurrentTurn raw hash `4b7d5c43265bcd166ec5cd901f6417b6bb8b73ed906465ee0de100be5085585c`，PASS
- SemanticEdits raw hash `16866d933e43fde4f43d9964483838334b83680becc43d8d77481f1dc72abb7b`，PASS

**CONSUMED PARSE / DRAFT**：与 raw response 解析结果一致。

**FINAL GUARD**：`app/semantic_v2/recognition.py::_check_semantic_coverage:452` 返回 `V2_PAYLOAD_WOULD_DROP_SEMANTICS`。

**FIRST DIVERGENCE**：coverage checker 没有承认 METADATA `targets` 已保存 subject。

**ROOT CAUSE**：`PAYLOAD_COVERAGE_MAPPING_MISMATCH`。不是 Source Value、Role hypothesis 或 Draft escalation。

## RB50-10

**原问题**：查询外周插管中心静脉导管合作的医院名单。

**CURRENT TURN RESULT**

- `m-0`：`外周插管中心静脉导管合作的医院`，span `[2,17)`，roles=`[SUBJECT_ENTITY]`，explicit=true
- explicit slot：subject；query shape=`RELATION_LIST`
- CurrentTurn 没有授权 `FILTER_VALUE`，也没有 filter_expression slot/marker

**SEMANTIC EDIT RESULT**

- subject SET 选择 hospital entity
- 额外生成 `req-product-name` 和 filter_expression ADD
- request 的 field handle 实际是同一 mention 的 `SUBJECT_ENTITY / ENTITY / product`，不是获授权的 FILTER_FIELD
- request 被 filter operand 消费，因此不是 orphan

**DYNAMIC SCHEMA / RAW MODEL RESPONSE / SCHEMA VALIDATION**

- CurrentTurn raw hash `973855914a43e659473fb472c7c3b233f9a004b650515a6e11c5113c0c551bbf`，PASS
- SemanticEdits raw hash `a65b656dac2b5ecb431dd8e299710a5b99343e4cf75c1d9a482d48dc49b63a69`
- 下发 Schema 明确包含 `source_value_requests.maxItems=0` 和 `field_binding_handles.maxItems=0`
- raw response 对两项都给出长度 1，动态 Schema FAIL
- 通用静态模型校验 PASS

**CONSUMED PARSE / DRAFT**：消费 Draft 与 raw response 解析结果完全一致；入口没有按已下发动态 Schema fail closed。

**FINAL GUARD**：`app/semantic_v2/source_value_recognition.py::resolve_requests:53` 发现 current mention 不含 `FILTER_VALUE`，拒绝 `V2_SOURCE_VALUE_CURRENT_MENTION_REQUIRED`。

**FIRST DIVERGENCE**：`SEMANTIC_EDITS_RAW_RESPONSE_VIOLATES_ISSUED_DYNAMIC_SCHEMA`。

**ROOT CAUSE**：raw 模型响应违反动态 Schema；客户端只执行静态 Pydantic 校验，使违规 Draft 进入 Runtime。`DRAFT_ROLE_ESCALATION` 是违规内容，current-mention guard 是正确的后续拒绝。

## RB50-37

**原问题**：切换话题：查询耐高压植入式给药装置及附件的含税销售总额。

**CURRENT TURN RESULT**

- `m1`：耐高压植入式给药装置及附件，span `[7,20)`，roles=`[SUBJECT_ENTITY]`
- `m2`：含税销售总额，span `[21,27)`，roles=`[MEASURE]`
- markers：subject SET←m1，metrics SET←m2；query shape=`SCALAR_AGGREGATE`
- CurrentTurn 没有授权 m1 为 `FILTER_VALUE`

**SEMANTIC EDIT RESULT**

- subject SET 为 product；metrics SET 为 sales_total_including_tax
- 额外生成 `req_product_name` 和 filter_expression ADD
- request field handle 仍为 m1 的 `SUBJECT_ENTITY / ENTITY / product`，request 被 filter operand 消费

**DYNAMIC SCHEMA / RAW MODEL RESPONSE / SCHEMA VALIDATION**

- CurrentTurn raw hash `9e1719d97abfdca72e5b3fd4232a6cdc940264957220472bc0736ea4a5913fc4`，PASS
- SemanticEdits raw hash `61f2b65714d50cdc5f4e54bcb82943bf2f60b1215735594bdf05c324ec1f8300`
- 下发 Schema 同样要求 `source_value_requests.maxItems=0`、`field_binding_handles.maxItems=0`
- raw response 均返回 1 项；动态 Schema FAIL，静态模型校验 PASS

**CONSUMED PARSE / DRAFT**：与 raw response 解析结果完全一致；SemanticEdits 输入 hash 与原 Round 1 调用一致。

**FINAL GUARD**：与 RB50-10 相同，`resolve_requests:53` 返回 `V2_SOURCE_VALUE_CURRENT_MENTION_REQUIRED`。

**FIRST DIVERGENCE**：与 RB50-10 相同，为 `SEMANTIC_EDITS_RAW_RESPONSE_VIOLATES_ISSUED_DYNAMIC_SCHEMA`。

**ROOT CAUSE**：与 RB50-10 共享 `MODEL_SCHEMA_VIOLATION + DYNAMIC_SCHEMA_ENFORCEMENT_GAP` 机制。

## RB50-10 / RB50-37 工程边界判定

| 候选 | 判定 | 证据 |
|---|---|---|
| A. 模型没有遵守动态 Schema | **YES** | 两个 raw response 都生成 Schema 明确禁止的 Source Value request 和 field handle。 |
| B. 客户端没有在入口拒绝 | **YES** | `RecognitionModelClient.complete` 最终只执行 `output_model.model_validate_json(content)`；静态模型允许最多20个 request。 |
| C. SemanticEdits 没有使用预期动态 Schema | **NO** | 从实际 system message 的 `JSON Schema:` 段解析出的对象与保存的动态 Schema逐字段相同；hash 分别与调用记录一致。 |
| D. response parsing / validation 没应用动态 Schema | **YES** | 通用 Pydantic 校验 PASS，而把同一 raw response 对 exact issued Schema 校验则命中两个 `maxItems` 错误。 |
| E. 其他机制 | **补充事实** | 当前调用的 `response_format` 是 `json_object`，Provider 不承担 strict JSON Schema 执行；动态 Schema以 system message 下发，故客户端本地验证不可省略。 |

唯一的第一分歧发生在 SemanticEdits raw response：**模型输出首先违反已下发 Schema**。最早的工程执行缺口紧随其后：**客户端没有使用 exact issued schema 验证响应**。后面的 current-mention guard 没有制造根因，它正确阻止了未授权 role 被消费。

## SHARED_ROOT_CAUSE_CANDIDATE

**SHARED_ROOT_CAUSE_CANDIDATE = RB50-10 + RB50-37**

- 相同 first divergence：`SEMANTIC_EDITS_RAW_RESPONSE_VIOLATES_SOURCE_REQUEST_MAXITEMS_ZERO`
- 相同机制：动态 Schema 禁止 Source Value request；raw 模型响应仍生成 request；静态 `SemanticTaskDraft` 接受；客户端不校验 exact issued schema；Runtime 后续在同一 guard fail closed
- 相同代码修复点：`app/semantic_v2/recognition_client.py::RecognitionModelClient.complete`，当前返回点约第61行
- RB50-25 也共享 Schema 违规与入口执行缺口，但因 request 未消费，后续在 `resolve_requests:49` 以不同 guard 拒绝

这是对**定向新诊断调用**的共享根因证明。它不证明原正式 Benchmark 的缺失正文必然逐字相同，也不证明修复后模型会生成正确语义。

## PROPOSED_MINIMAL_FIX

在 `RecognitionModelClient.complete` 中，收到 content 后、构造并返回 `output_model` 前，用调用时的 **exact issued schema** 对原始 JSON 实例做本地严格校验：

1. JSON 解析失败或不满足 exact schema，立即返回稳定的系统错误 reason；
2. 不删除违规字段，不把 SUBJECT 自动改为 FILTER，不补写 role；
3. 不重试模型，不增加第三阶段，不放宽 Scope、mention、binding 或 source-value guards；
4. exact schema 校验通过后，再执行现有 Pydantic output model 校验。

该最小修复的直接收益是让 Schema 违规在入口 fail closed，并获得准确的错误归因。它**不会**让 RB50-10/37 自动变成正确计划，也没有证据会提升 5/37 或 2/30；若以后需要修正模型语义生成，必须另有独立授权和正反例，不能在本轮用 Prompt 或 role 强制转换实现。

本轮按要求不执行该修改。

## 最终状态

- `TARGETED_FIRST_DIVERGENCE_REPORT`：COMPLETE
- `SHARED_ROOT_CAUSE_CANDIDATE`：YES，RB50-10 / RB50-37
- `PROPOSED_MINIMAL_FIX`：exact issued dynamic-schema validation at recognition client boundary
- Production code changes：0
- Prompt changes：0
- New model calls after recovery：0
- Core50 / V1 / SQL / 8088 runs：0
- Benchmark score changes：0
- 下一动作：停止，等待后续任务；不自动实施修复
