# RB50-37 Offline Replay

**ROUND24_OFFLINE_ENTITY_CLOSURE_PASS**

离线回放精确复用 Round 2.2 已保存的 CurrentTurn 与 SemanticEdits 响应，并注入本轮从真实源取得的 frozen exact observation。外部模型调用为 0，SQL 调用为 0，生产写入为 0。

| 验收项 | 结果 |
|---|---|
| TASK_PUBLISHED | PASS |
| SPECIFIC_ENTITY_PRESERVED | PASS |
| SOURCE_VALUE_PROOF_VALID | PASS |
| PAYLOAD_PRESERVED | PASS |
| CANONICAL_PRESERVED | PASS |
| COMPLETED_QUESTION_PRESERVED | PASS |
| NO_SILENT_BROADENING | PASS |
| METRIC_PRESERVED | PASS |

最终 Task version 为 1，Payload 为 `SCALAR_AGGREGATE`。TaskState、Payload filter、Canonical Request 和 completed question 消费同一个受 Scope/Catalog Pin 约束的 `product_name` 精确值证明；没有退化为全部产品范围。

本结果证明 Round 2.3 的具体实体保留逻辑在拥有真实精确 proof 时可以闭环。它不代表新的模型响应一定会生成相同的合法 Draft，也不更新 Core50 Benchmark 分数。

PRIVATE 回放 receipt SHA-256 为 `f2b0cd4ceb4b6051e38aee6830b93f4cb761886dd9ec0165ef1f2d49d7cd4b9c`。离线 Gate receipt SHA-256 为 `01ef5ddc3bd121452409981d53a53fd494bf9fda6ad0a9f2a6a1571a2942fcdd`。

与本轮范围相关的专项和负向控制共 **219 passed / 0 failed / 0 collection errors**。其中 Generic“医院”仍作为 Subject/Dimension，未被转换为 `hospital_name = 医院`；Subject + concrete product instance 的正向合同也保持通过。
