# V2 Context Basic Task Publication Regression Closure

**V2_BASIC_NEW_TASK_AND_FOLLOWUP_EXECUTION_COMPLETE**

本轮只修复 `MODEL_WIDE` 请求下的 Source Value 回执范围校验。没有引入 Execution Anchor、DemoExecutionEnvelope、Catalog Pin 门禁或迁移、Execution Scope 物化、V1 执行补丁，也没有修改 Oagent、SQL Translator、Java/platform、8088 路由或 Redis schema。

## FIRST_DIVERGENCE

真实 UTF-8 问题为“查询去年江苏省订单笔数”。当前 slim 实现只执行了 1 次 CurrentTurn 模型调用，HTTP 200、retry=0；没有调用 SemanticEdits、SQL 或写 Redis。失败前的 CurrentTurn 与旧成功实现一致，但 Source Value 回执校验错误地把两种范围合同视为同一个对象：

- 请求授权范围：semantic model 81、`business_domain_ids=[]`、`MODEL_WIDE`；
- 当前 Catalog 已解析范围：semantic model 81、`business_domain_ids=[205]`、`EXPLICIT_DOMAINS`；
- Source Value 回执范围：与当前 Catalog 已解析范围相同，为 `[205]`。

回执的 source、exact match、字段身份、Catalog 身份、mapping hash、observation hash 和完整性全部通过。唯一失败项是回执 `[205]` 与请求授权 `MODEL_WIDE []` 不相等；它与已验证的当前 Catalog 范围完全相等。

`FIRST_DIVERGENCE = SOURCE_VALUE_RECEIPT_SCOPE_VALIDATION`

`FALLBACK_REASON = V2_SOURCE_VALUE_RECEIPT_MISMATCH`

随后 generic standalone NEW_TASK passthrough 把该具体错误转换成 `V1_EXECUTION_FALLBACK_NEW_TASK`，所以首轮 V1 能执行，但 ConversationState 只有 version 1、Task 数为 0。passthrough 本身没有制造根因；它隐藏了基础 V2 计划失败。

## OLD SUCCESS 与 CURRENT SLIM

旧成功证据取自提交 `6b43ce6` 附近已经落盘的原接口验收。旧调用使用显式 `[205]`，因此请求授权范围和 Catalog/Source 回执范围天然相等；它完成 CurrentTurn、SemanticEdits、Plan 和 TaskState。当前 slim 的 CurrentTurn 对同一文本仍正确，首个行为差异只出现在 `MODEL_WIDE` 请求经当前 Catalog 解析为 `[205]` 后的 Source Value 回执范围比较。

| 阶段 | 当前修复前 | 修复后 |
|---|---|---|
| 1. CurrentTurn Parse | PASS | 同一录制响应 PASS |
| 2. relation / dialogue act | `NEW_TASK / ACCEPTED` | 不变 |
| 3. mentions | 去年=`TIME_RANGE`；江苏省=`FILTER_VALUE`；订单笔数=`MEASURE` | 不变 |
| 4. operation markers | raw marker 为空；三个 explicit slot 在首任务初始化时按 `SET` 处理 | 不变 |
| 5. query shape | `SCALAR_AGGREGATE` | 不变 |
| 6. Catalog metric candidates | 包含 `order_count / 订单笔数` | 不变 |
| 7. metric binding | 尚未发布 | `order_count / 订单笔数` |
| 8. 江苏省 Source Value | 第一个精确读取回执在范围比较处被拒绝 | 在已解析 `[205]` Catalog 范围内找到精确值 |
| 9. filter binding | NOT REACHED | `province_name EQ 江苏省`，带真实 Source Value proof |
| 10. time expression | `去年` 已识别 | 不变 |
| 11. normalized time | NOT REACHED | Asia/Shanghai 的 2025 左闭右开区间 |
| 12. SemanticEdits | NOT REACHED | Fully Determined Fast Path 完成，按设计无需 SemanticEdits |
| 13. Plan result | 无 | `AuthorizedLogicalPlan / SCALAR_AGGREGATE` |
| 14. next ConversationState | version 1，tasks=0 | version 1，tasks=1 |
| 15. TaskState | 未创建 | v1：订单笔数、江苏省、2025 |
| 16. passthrough | `V1_EXECUTION_FALLBACK_NEW_TASK` | 不触发 |
| 17. precise reason | `V2_SOURCE_VALUE_RECEIPT_MISMATCH` | 无失败；completed question=`查询2025年江苏省订单笔数。` |

## 最小修复

`source_value_binding.lookup_values` 继续使用请求的 `authorized_scope` 做授权和字段检查，但用 `ScopedPlanSession` 已经验证过的 `resolved_business_domain_ids` 构造本轮物理 Catalog/Source 回执期待范围。显式 `[205]` 请求的行为不变；`MODEL_WIDE []` 也没有被改写为 V1 execution scope。没有放宽 source identity、字段映射、Catalog identity、hash 或 observation 完整性校验。

同一录制 CurrentTurn 的无模型重放在修复后生成 Task v1：metric=`order_count`、filter=`province_name EQ 江苏省`、time=2025，completed question 为“查询2025年江苏省订单笔数。”。新增双轮回归进一步证明“换今年”复用同一 Task、active version 变为 2、metric/filter 不变、time operation 为 `REPLACE`，completed question 为“查询2026年江苏省订单笔数。”。

## 验证

- 精确专项：7 passed；覆盖基础双轮、Source 回执范围、复杂 unsupported NEW_TASK passthrough、passthrough barrier、Catalog A→B 状态延续及纯 V1/Bridge 字段一致性。
- 受影响集合：659 passed / 1 existing failed / 0 collection errors。
- 与既有 595 个可比较节点对比：old-pass→new-fail=0；唯一失败 node 与基线完全相同，为 `test_rejected_mixed_patch_leaves_state_barriers_versions_and_message_unchanged[slot_conflict]`。
- 新增或本轮额外纳入的 65 个节点全部通过。
- 模型调用：诊断 1 次 CurrentTurn；修复后重放新增模型调用 0；SemanticEdits 0。
- SQL、Benchmark、生产写入、8088 重启：0。

PRIVATE 证据位于 `.eval_private/basic-new-task-taskstate/`。原始模型正文、Source Value 回执和业务数据不进入 Git。受影响 JUnit 位于 `.eval_private/v2-context-v1-execution-slim/basic-task-publication-affected-final.xml`。

以上为离线修复和回归 Gate；下节记录随后完成的真实平台验收。

## 真实平台双轮验收

修复提交加载到 8088 后，真实平台在同一会话先后发送“查询去年江苏省订单笔数”和“换今年”。两次请求均经过原 `/agent_chat/stream`，HTTP 200；8088 保持 `/live=200`、`/ready=READY`，Oagent 与 SQL Translator 进程未重启。

首轮不再进入 `V1_EXECUTION_FALLBACK_NEW_TASK`，而是以 `V2_RESOLVED_COMPLETED_QUESTION` 发布一个 Task v1。Task 保存订单笔数、江苏省精确过滤和 2025 年时间范围；随后原 V1、Oagent、SQL Translator 和数据库均成功，响应为 `COMPLETED`，同时具有 Oagent ASL 和 `data-source` 查询结果证据。

第二轮读取同一 Redis conversation key 和同一 Task，`active_version` 从 1 变为 2；订单笔数与江苏省保持不变，时间通过 `REPLACE` 更新为 2026，实际送入 V1 的完整问题为“查询2026年江苏省订单笔数。”。它仍使用 `V2_RESOLVED_COMPLETED_QUESTION`，没有触发 standalone passthrough，证明本轮“基础 NEW_TASK 没有建立 TaskState”的回归已在真实运行态关闭。

第二轮下游没有完成：V1 调用了 Oagent 两次，Oagent 服务日志均记录 ASL 已生成；返回的 ASL 仍含业务歧义，DataAnalysis 的既有 Adapter 在 SQL 翻译前以 `ASL_AMBIGUOUS` 拒绝。Redis 公开响应只持久化 `SAFE_FALLBACK` 和 `source_stage=OAGNET_ASL_GENERATION`，没有保留 ambiguity 正文或上游 error code；`ASL_AMBIGUOUS` 由该 source-stage 对应的唯一代码分支确定。SQL Translator 日志仅有本轮指标解析调用，没有后续 `/api/translate`，数据库也没有收到第二轮查询。

| 验收项 | 结果 |
|---|---|
| 基础首问创建 V2 Task | **PASS**；Task v1，未 fallback |
| 首问 V1 → Oagent → SQL → DB → 页面 | **PASS** |
| “换今年”复用同一 Task | **PASS**；Task v2 |
| 时间替换与 completed_question | **PASS**；2026，`查询2026年江苏省订单笔数。` |
| 第二轮 V1 execution | **REACHED** |
| 第二轮 Oagent | **REACHED**；两次 ASL generation |
| 第二轮 SQL translation / DB | **NOT_REACHED** |
| 第二轮页面业务结果 | **FAIL**；`ASL_AMBIGUOUS` → `SAFE_FALLBACK` |

`FIRST_FAILURE_STAGE = OAGNET_ASL_RESPONSE_AMBIGUITY_VALIDATION`

该新失败位于原 V1/Oagent 执行链，不是 TaskState、completed_question、稳定 conversation identity、Catalog 或 passthrough 回归。本轮按范围要求停止，不修改 V1、Oagent、SQL Translator 或 Bridge，也不运行 Benchmark。

## Oagent ????

????? `OAGNET_ASL_RESPONSE_AMBIGUITY_VALIDATION` ??????????4 ? Oagent ?????? ASL ??????SQL ??????? 0????????????????????????????????????????????????? `dim_province.province_name = ???`??????? ambiguity ??????????????????? ambiguity??? DataAnalysis ??? ASL ???? `ASL_AMBIGUOUS`?

???? Oagent ? Intent-ASL ?????????????????????????????????????/?????? filter ambiguity????????????????????? fail closed????? V2 Context?Bridge?V1?SQL Translator?Scope?Catalog ? Redis?

??????? **697 passed / 1 existing failed** ???? **701 passed / 1 same existing failed**?4 ?????????`old-pass?new-fail=0`??????/????? **107 passed / 1 same existing failed**??????? node ???????????

??? Oagent 8021 ???????????? 8088?

| ?? | V2 ?? | Oagent ?? | SQL / DB | ?? |
|---|---|---|---|---|
| ??????????? | Task v1?2025????????? | PASS????? ambiguity ??? | REACHED / REACHED | HTTP 200 / COMPLETED |
| ??? | ?? Task v2????? REPLACE ? 2026?completed question=`??2026?????????` | PASS????? ambiguity ??? | REACHED / REACHED | HTTP 200 / COMPLETED |

Redis ????? state version 2??? Task?versions `[1,2]`?m1/m2 ?? `V2_RESOLVED_COMPLETED_QUESTION` ? `V1_EXECUTION_RESPONSE_SAVED`?Oagent ? PID ? 28896?DataAnalysis 35348?SQL Translator 26172 ?????????? `oagnet-asl` ? `data-source:58` ?????????????

`FIRST_FAILURE_STAGE = CLOSED_AT_OAGENT_CONTRACT_REPAIR`

?????????? [asl_ambiguity_solution.md](asl_ambiguity_solution.md)?
