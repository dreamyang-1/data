# 意图与结构化抽取混淆分析

## 核心判断

当前 `METRIC_QUERY/DETAIL_QUERY/TREND_ANALYSIS/...` 是复合标签，混合了至少四个独立问题：用户在做什么对话动作、应路由哪个服务、分析目标是什么、查询结果是什么形态。模型即使选对一个标签，也可能在其他轴上出错。

## 已观察混淆

- **OBSERVED_FAILURE**：3例明细/指标形态混淆（real-002/005/022）。
- **OBSERVED_FAILURE**：5例 ADD 型追问未正确合并（real-001/004/014/015/021）。
- **OBSERVED_FAILURE**：5例“只看前N/排序”等数据集操作被错误重新规划（real-017/020/027/029/030）。
- **OBSERVED_FAILURE**：扩展测试18项失败集中在澄清字段去重、模型字段落地约束、PendingState 转换和中断恢复。

## 建议多轴Schema

- `DialogueAct`：NEW_REQUEST、FOLLOW_UP、CLARIFICATION_ANSWER、CORRECTION、REFRESH、REVISE、CANCEL、CHAT。
- `ServiceRoute`：SEMANTIC_QUERY、DATASET_TRANSFORM、ANALYSIS、REPORT、CHAT。
- `AnalysisGoal[]`：LOOKUP、AGGREGATE、TREND、RANK、COMPARE、COMPOSITION、CORRELATION、ANOMALY。
- `QueryShape`：DETAIL、AGGREGATE、TIME_SERIES、TOP_N、COMPARISON、MULTI_TASK。
- `SemanticRole`：ENTITY、METRIC、DIMENSION、ATTRIBUTE、ENTITY_VALUE、RELATION、TIME_GRAIN、RELATION_TARGET、DATASET_OPERATION。

旧 `intent` 在迁移期只作为派生兼容字段，不再作为所有决策的唯一来源。详细混淆计数见 `intent_confusion_matrix.csv` 和 `slot_role_confusion_matrix.csv`。

