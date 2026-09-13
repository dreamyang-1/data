# V2 Context Basic Task Publication Regression Closure

**V2_BASIC_NEW_TASK_PUBLICATION_OFFLINE_PASS**

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

当前状态只证明离线修复和回归 Gate。下一步只重启 DataAnalysis 8088，并从真实平台新会话验证指定的两轮基础问题。
