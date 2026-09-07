# Phase 2 修正与补遗

1. “时间粒度未进计划”修正为 `TIME_GRAIN_PROPAGATION_OR_PROOF_FAILURE`：ASL 已有 `dimensions[].granularity`，但跨层传播与结果证明不稳定。
2. “指标规范化 IMPLEMENTED”修正为 `MECHANISM_IMPLEMENTED_CORRECTNESS_UNPROVEN`。
3. 30 个失败案例归因是高概率首错层，不是在线确定性全链路重放。
4. TypedLogicalPlan v0.1 标记为 `CONCEPT_DRAFT_NOT_PRODUCTION_READY`。
5. 指标—维度绑定名单不等于 Join 粒度安全证明。
