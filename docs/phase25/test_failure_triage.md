# 测试失败分类与处理

## 结果

- Phase 2 记录：1253 passed / 25 failed / 2 collection errors。
- Phase 2.5 初次全量收集：5 collection errors。
- 当前全量：1452 passed / 32 failed / 0 collection errors。
- 新增 V2 契约：34 passed。

## 已修复

修复当前代码仍真实引用的 Schema、ExtensionExecution、内部语义快照和 SQL-free recall store 契约。当前全量中 7 个原失败断言转为通过，5 个收集错误模块全部恢复。未恢复废弃执行路径、未扩大 Any、未删除或弱化断言。

## 未修复

澄清/Pending、事件注入、OpenAPI 历史断言、状态策略和关系投影仍需逐项合同确认；本轮不以大范围生产链改动换取绿色测试。详见 JSON。
