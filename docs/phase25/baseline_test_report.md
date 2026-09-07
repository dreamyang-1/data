# Phase 2.5 基线测试报告

## 证据边界

- Phase 2 已记录基线：1253 passed、25 failed、2 collection errors。
- Phase 2.5 开始时重新执行全量收集，实际发现 5 个模块收集错误：缺少 `AnalysisStep`、`ResultValidationReport`、`BusinessRuleRef`、`SemanticSqlValidationCheck` 等当前源码仍在引用的契约。
- 初次全量测试因收集失败中止；该结果不能伪装成已执行 1479 个测试。
- 本轮只使用本地 fixture、Mock 与确定性实现；没有真实模型、外部网络或外部存储写入。
- 当前跨仓隔离回归：Oagnet 210 passed；SQL Translator 101 passed。

## 失败分类原则

收集错误归为 SCHEMA_DRIFT。Phase 2 的 25 个失败按 SCHEMA_DRIFT、STALE_TEST、REAL_BEHAVIOR_BUG、MOCK_DRIFT、MISSING_FIXTURE、BUSINESS_RULE_UNKNOWN、ENVIRONMENT_DEPENDENCY、UNKNOWN 分类，详见 `test_failure_triage.json`。不明确的业务预期不会通过弱化断言消除。
