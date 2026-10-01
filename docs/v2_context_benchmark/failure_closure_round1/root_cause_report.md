# Real-Business Failure Closure Round 1 — Root Cause Report

**REAL_BUSINESS_FAILURE_CLOSURE_ROUND1_PARTIAL**

本轮以冻结的 Core 50、Git 基线 `d9e9a37df76e86af14dea2a1c5df52c9621e5f57` 和共同可评分集 37 条为准。修复范围严格限制在 `Source Value → Semantic Binding → Edit / Filter Consumption → Task Publication`。没有修改 Core 50、期待、评分规则、Prompt、Catalog、Scope、Oagnet、SQL Translator、Redis、Session 或 V1 路由。

## 已证明的第一分歧

真实调用链中存在三个可通用修复的表示缺口：

1. `SourceValueRequestDraft.mention_id` 可能指向另一个 offered object，而它选中的字段 handle 与消费它的 edit 同时、唯一地指向当前显式 `FILTER_VALUE` mention。原校验随后以 `V2_SOURCE_VALUE_CURRENT_MENTION_REQUIRED` 拒绝。
2. Source Value 请求选中的 Catalog 字段身份正确，但 handle 的 mention 分量不是当前值 mention；当前候选集合中存在同一 Catalog identity、同一 `FILTER_FIELD` role、当前 mention 的唯一精确 handle。原校验随后以 edit evidence / binding identity 不一致拒绝。
3. 空的新任务中，模型已经声明 Source Value request，却漏掉承载它的初始 Filter edit。只有当每个请求都未被消费、每个请求都有唯一当前字段 handle、当前 parse 对同一 mention 明确声明 `filter_expression ADD`、不存在竞争 Filter 结构时，缺失内容才是可证明的结构包装，而不是新增业务语义。

这些结论来自原始模型产物、现有 handle registry、parse 的显式 slot/operation evidence 和拒绝边界，不来自具体商品、地区、医院、品牌名称或 Case ID。

候选版本在 37 条共同可评分 Case 中的错误族聚类如下。Case 数是最终结果的主错误，不与 102 轮事件数混用：

| Error family | Case count | First divergence | Shared root cause |
|---|---:|---|---|
| `V2_SOURCE_VALUE_CURRENT_MENTION_REQUIRED` | 2 | SOURCE_VALUE_RESOLUTION / CURRENT_EVIDENCE_VALIDATION | Parse 没有提供请求所需的当前 `FILTER_VALUE` role，或请求 pointer 未能由当前值证据闭合 |
| `V2_BINDING_OUTSIDE_EDIT_EVIDENCE` | 3 | EDIT_EVIDENCE_VALIDATION | binding 被用于目标 slot，但对应 edit evidence 没有声明同一语义用途；Ranking 样本还暴露后续 Query Shape 缺口 |
| `V2_SOURCE_VALUE_REQUEST_NOT_APPLIED` | 3 | SOURCE_VALUE_RESOLUTION / REQUEST_CONSUMPTION | Source Value request 没有进入完整 Filter edit，或存在不能安全覆盖的竞争 operation |
| `V2_PAYLOAD_WOULD_DROP_SEMANTICS` | 1 | TASK_PUBLICATION / CANONICAL_PAYLOAD_VALIDATION | 上游已声明的显式语义仍未进入最终 payload，发布 Guard 正确拒绝 |

其余失败在本轮的 Source Value P0 修复之后才分类，不把 downstream `V2_CONTEXT_UNRESOLVED` 误算为同一个根因。

## 最小生产修改

| 修改 | 目的 | 确定性条件 | 失败方式 |
|---|---|---|---|
| 当前 request mention 对齐 | 修正被复制到错误对象的 mention pointer | 字段 handle 与所有消费 edit 唯一指向同一个当前显式 `FILTER_VALUE` | 任一证据不一致则不修复 |
| 字段 handle 精确重绑定 | 保留已选 Catalog identity，只替换 mention 分量 | 同 Catalog identity、同 role、当前 mention 的 offered handle 恰好一个 | 缺失或多候选则不修复 |
| 初始 Filter edit 补齐 | 表达模型已声明但漏包裹的当前值过滤 | 空新任务、base version 0、全部请求未消费、全部为显式 ADD、无竞争 Filter edit | SET、旧任务、歧义、未解析 mention 均拒绝 |
| 生成 Schema 收紧 | 阻止模型把 source request 指向非当前值 mention | `mention_id` 只能来自当前显式 `FILTER_VALUE`；没有此类 mention 时禁止请求 | Runtime 严格校验继续保留 |

`SET` 没有被转换或合成，因为完整替换需要完整 Boolean operand；自动合成会改变既有状态语义。修复不选择 Catalog candidate、不推断 source value、不创建 task target、不扩大 Scope，也不删除未消费 mention。

## 正反例与安全边界

新增测试覆盖了精确对齐、同 identity handle 重绑定和空任务 ADD 初始化；反例覆盖错误 identity、错误 role、多候选、非当前 mention、未解析 mention、旧任务、竞争 Filter edit、重复 request 以及 SET。所有反例继续由原有 Guard 拒绝。

三条保存产物的离线诊断使用版本化 recorded-output oracle migration，因为旧 capture 的 Context 输入合同早于当前版本。它们没有外部调用，也不计模型准确率：

- `G81-074` 仍以 `V2_SOURCE_VALUE_REQUEST_NOT_APPLIED` 拒绝，证明竞争 CLEAR 不会被自动修复。
- `G81-080`、`S81-007` 越过原 `REQUEST_NOT_APPLIED` 边界，停止在冻结 source observation / targeted lookup 证据缺口；没有被计为 Gold PASS。

正式 Core 50 重跑后，没有新 Case 达到完整评价标准。所有 102 轮中四个 P0 错误码事件合计从 31 降到 29，但首轮 Task 发布仍为 2/30，计划产出仍为 13/102，`CASCADE_RECOVERED=0`。因此这是一项已验证的局部合同修复，不是 Real-Business Failure Closure 完成。

## 后续根因边界

UTF-8 诊断显示，部分真实输入在 CurrentTurn Parse 中只被声明为 `SUBJECT_ENTITY`，而后续 Draft 又把同一对象作为 Source Value filter 使用。当前 Guard 正确要求 `FILTER_VALUE` 的当前证据。将 SUBJECT 自动升级为 FILTER_VALUE 会发明角色语义，本轮没有这样做。

Ranking 诊断显示同一个 metric binding 被用于 metrics 与 `rank_by`，但 ranking edit evidence 只包含 limit mention。一次未提交的精确 evidence 实验只把失败推进到缺少 `group_by` 的 Query Shape/IR 边界，并未得到正确计划；该实验已撤回，没有进入提交。

当前停止点是：下一分歧已进入模型跨阶段 role/edit 一致性、冻结 Source Value 观察可用性以及 Query Shape 结构，不再属于本轮可由精确 identity repair 安全关闭的范围。

## 结论

**P0_SOURCE_VALUE_IDENTITY_REPAIR = PASS_WITHIN_PROVEN_CONTRACT**

**REAL_BUSINESS_CORE50_IMPROVEMENT = NOT_ESTABLISHED**

**ROUND_1_STATUS = PARTIAL**

生产功能提交：`979b048f0fd86f61c8bae42208f4e30eb5a86b49`。
