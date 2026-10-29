# Round 2.3 Specific Entity First Divergence

**SPECIFIC_ENTITY_FIRST_DIVERGENCE = CURRENT_TURN_ENTITY_INSTANCE_LOST**

本结论只使用 Round 2.2 已保存的 RB50-37、RB50-10 capture 和既有 Generic Subject 结果，新增模型调用为 0。

## RB50-37 因果链

原问题为“切换话题：查询耐高压植入式给药装置及附件的含税销售总额。”。

| 阶段 | 已观察事实 | 判定 |
|---|---|---|
| Raw question | 明确给出具体产品名和指标 | 具体实体约束存在 |
| CurrentTurn | `m1` 保留完整 span `[8,20)` 和原文，但唯一角色为 `SUBJECT_ENTITY`，只声明 `subject SET` | **首次把实体实例压缩为实体类型** |
| Catalog candidates | `m1` 只得到产品 Entity Type 候选；没有产品名称 Attribute 或 Source Value 候选 | CurrentTurn 角色决定的后续效果 |
| SemanticEdits | 只生成 `subject=product` 和含税销售总额 metric | 具体产品约束已无可消费输入 |
| TaskState | subject 和 metric 存在，filter 为空 | 下游效果 |
| Payload | `SCALAR_AGGREGATE`，filter 为空 | 下游效果 |
| Canonical semantics | 查询范围为全部产品 | 静默语义扩大 |

因此分类 A 有直接证据。B–F 均发生在 A 之后，不能作为唯一 First Divergence。

## 两个对照

- RB50-10 的具体产品短语同样需要实例归宿，但还包含关系目标；Round 2.2 先在 Dynamic Schema 边界安全拒绝。本轮不扩展 Relation Query。
- 已有真实 Benchmark 中“统计每家医院承接的订单总金额（含税）及订单笔数，并关联医院等级”把“每家医院”正确保留为泛化 Subject/Dimension，filter 为空。新增离线负对照也证明“按医院统计……”不会触发实例查询。

## 现有表达能力

无需增加业务专用字段。现有模型已能完整表达：

- `TaskSemanticState.filter_expression`
- `Predicate(FILTER_FIELD = EntityValueRef)`
- `BoundSemanticRef` 的 Catalog/Scope/mention identity
- `SourceValueBindingEvidence`
- Payload `filters`
- Canonical Request 的 `filters` 与 `semantic_filter_bindings`

冻结 81/[205] 目录为 product、hospital、province 分别声明了单一主名称属性 `product_name`、`hospital_name`、`province_name`，且它们属于允许的 Entity Value lookup fields。目录声明只证明可查询字段，具体值仍必须由当前 Scope 下的 exact Source Value receipt 证明。

## 最小通用修复

新任务在模型已选择一个受 Scope 约束的 Entity subject 后，检查该 subject 的唯一当前显式 mention：

1. mention 与 Entity 的正式名称、编码或 alias 相同，保持 Generic Entity Type；
2. mention 具有 Group By 角色，保持 Subject/Dimension；
3. 其他 surface 只有在目录声明唯一主名称字段，并且 exact normalized Source Value lookup 返回唯一值时，才用已有 Filter/Proof 结构保存；
4. 缺字段、无匹配、多匹配、Scope/receipt 不一致时 fail closed；
5. 只处理 base version 0 的新任务，不读取历史实例，不改变已有任务和 CLEAR barrier；
6. TaskState 到 Payload 继续由既有 semantic coverage guard 检查，Canonical 映射继续消费同一 Plan。

新增的 `CURRENT_EXACT_ENTITY_INSTANCE` 操作和既有 `StructuredEditTrace` 记录该确定性补全。没有新 Prompt、业务词特判、模糊匹配、第三模型阶段或 Catalog ID 推断。

## 证据边界

合成但合同真实的离线 Catalog/Source fixture 已证明产品、医院、省份三类实例能够保留到 Task、Payload、completed question 和 Canonical Request。Round 2.2 冻结 Source Value artifact 没有 RB50-37 商品名的 exact observation，所以旧 capture 回放只能证明安全阻止扩大查询，不能证明该真实商品值已经正确发布。
