# 当前Schema到目标Schema映射

| 当前字段/状态 | 当前问题 | 目标字段 | 迁移方式 |
|---|---|---|---|
| `intent` | 混合服务路由、目标和形态 | `dialogue_act/service_route/analysis_goals/query_shape` | 双写并比较，旧值由新轴派生 |
| `entity`/业务实体值文本 | 类型和规范ID可能混杂 | `SemanticRef` + `ENTITY_VALUE` | 强制 canonical ID/code，保留 mention span |
| `metrics/dimensions/fields` | 角色靠字段容器隐含 | `typedProjection.role` | 目录校验后写入，不接受未命中文本 |
| `filters[].field/value` | field ownership 和 value provenance不足 | typed `filter.field` + provenance | 先字段角色，再实体值召回 |
| `time_range`/time policy | 锚点、粒度、边界分散 | `time.anchor/range/grain/boundary/timezone/source` | 统一时间对象，兼容适配 ASL time_context |
| `sorting` + limit | 可能与数据集操作混淆 | `sort[]/limit` + `service_route` | 路由先判定，计划内保持 typed ref |
| `relationship_anchor` | 非显式关系目标 | `RELATION_TARGET` + relation path | 目录路径可验证且回显 |
| `Dataset`/analysis contract | 只验证部分结构 | `result_contract` | 增加grain/required columns/order/row bounds |
| PendingState | 缺失槽位与任务版本耦合弱 | versioned task frame | reducer 每次产生版本和变更集 |

## 兼容原则

1. 新Schema先以 shadow 模式生成，不影响旧 ASL/SQL。
2. 只对两者等价的请求启用双读比较；差异记录 Trace，不自动切流。
3. 引入 `plan_version/catalog_version/task_version`，任何回退均可按版本进行。
4. REFRESH 保持原任务语义，REVISE 生成新任务版本；不得复用为普通追问。
5. 目标草案见 `typed_logical_plan_schema_draft.json`，它是 **PROPOSAL**，不是已实现接口。

