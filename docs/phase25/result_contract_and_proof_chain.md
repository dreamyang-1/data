# ResultContract 与证明链

计划生成 `ResultContract`，包含规范指标/维度/实体、列、时间粒度、顺序、行界、唯一键、基数、空/截断策略、快照、水位、质量、数值约束和证明要求。证明顺序为 `PlanContract → ASLContractProof → SQLPlanProof → ResultProof`。任一阻塞证明为 FAIL 或 UNKNOWN 时不得返回 COMPLETED。`app/semantic_v2/result_contract.py` 提供结果结构的确定性证明函数。
