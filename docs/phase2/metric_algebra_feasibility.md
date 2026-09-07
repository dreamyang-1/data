# 指标语义代数可行性

## 当前事实

- 业务域205共有11个指标，均有计算公式、单位、来源实体和维度绑定。
- 公式可以观察到 SUM、COUNT、比率等运算，但目录没有独立的聚合类型和可加性字段。
- SQL翻译器对已知1:N放大风险会拒绝，证据为 `sql_translator_prod.py:2940-2969`；当前没有自动预聚合。

## 可行的最小代数

每个指标定义至少需要：`base_expression`、`aggregation`、`distinct_key`、`additivity`、`allowed_grains`、`time_anchor`、`unit`、`null_policy`、`negative_value_policy`、`global_filters`、`source_grain`。派生指标还需要 numerator/denominator、除零规则与聚合顺序。

| 类型 | 可加性 | 安全规则 |
|---|---|---|
| 金额/数量SUM | 通常时间和实体可加 | Join前保持事实粒度，必要时预聚合 |
| COUNT | 依赖计数键 | 明确 COUNT 与 COUNT DISTINCT |
| 比率 | 不可直接相加 | 聚合分子分母后再除 |
| 存量/余额 | 时间半可加 | 需快照或期末规则 |
| 时长 | 依业务口径 | 明确对象粒度和重复记录策略 |

## 判断

- **PROPOSAL**：可以在现有目录上增量引入指标代数，不需要立刻推翻现有公式。
- **RISK**：仅解析 SQL 公式不能可靠判断业务可加性。
- **UNKNOWN**：11个指标逐项的业务可加性、去重键及退货口径仍需指标负责人确认。

