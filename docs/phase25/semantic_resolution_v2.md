# 语义解析 V2

固定流程：Mention Span → 多 Role Hypothesis → 各角色 N-best → 作用域/归属/关系可达/指标维度/权限/版本/基数约束 → 完整可执行计划评分。模型只能选择候选 ID、UNRESOLVED 或 AMBIGUOUS，不能创建目录 ID。只有两个不同且均可执行的完整计划仍不可区分时才允许用户消歧。选中项以目录规范值覆盖模型自由文本，原词仅留 provenance。

拒绝原因枚举包括 scope、role、field ownership、relation、metric-dimension、permission、catalog version、duplicate relation、score、grain、join cardinality 和 unknown business rule。
