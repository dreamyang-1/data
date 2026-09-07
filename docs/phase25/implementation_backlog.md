# 正式实施 Backlog

| ID | 阶段 | 仓库 | 组件/文件 | 依赖 | 工作内容 | 测试 | 验收标准 | 回滚方式 | 风险 | 跨仓库 | 优先级 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| B-001 | P0 | DataAnalysis/Oagnet/sql-translator | 身份隔离键审计 | 前序阶段 | 按规格实现并保留旧链 | 单测+Shadow+回放 | 质量门禁通过且可回滚 | 关闭feature flag | 跨仓语义漂移 | 是 | P0 |
| B-002 | P0 | DataAnalysis/Oagnet/sql-translator | 目录版本冻结 | 前序阶段 | 按规格实现并保留旧链 | 单测+Shadow+回放 | 质量门禁通过且可回滚 | 关闭feature flag | 跨仓语义漂移 | 是 | P0 |
| B-003 | P0 | DataAnalysis/Oagnet/sql-translator | Trace V2双写 | 前序阶段 | 按规格实现并保留旧链 | 单测+Shadow+回放 | 质量门禁通过且可回滚 | 关闭feature flag | 跨仓语义漂移 | 是 | P0 |
| B-004 | P0 | DataAnalysis/Oagnet/sql-translator | 测试基线门禁 | 前序阶段 | 按规格实现并保留旧链 | 单测+Shadow+回放 | 质量门禁通过且可回滚 | 关闭feature flag | 跨仓语义漂移 | 是 | P0 |
| B-005 | P1 | DataAnalysis/Oagnet/sql-translator | 多轴解析Shadow | 前序阶段 | 按规格实现并保留旧链 | 单测+Shadow+回放 | 质量门禁通过且可回滚 | 关闭feature flag | 跨仓语义漂移 | 是 | P1 |
| B-006 | P1 | DataAnalysis/Oagnet/sql-translator | Mention Span | 前序阶段 | 按规格实现并保留旧链 | 单测+Shadow+回放 | 质量门禁通过且可回滚 | 关闭feature flag | 跨仓语义漂移 | 是 | P1 |
| B-007 | P1 | DataAnalysis/Oagnet/sql-translator | Slot Reducer | 前序阶段 | 按规格实现并保留旧链 | 单测+Shadow+回放 | 质量门禁通过且可回滚 | 关闭feature flag | 跨仓语义漂移 | 是 | P1 |
| B-008 | P1 | DataAnalysis/Oagnet/sql-translator | TaskVersion Shadow | 前序阶段 | 按规格实现并保留旧链 | 单测+Shadow+回放 | 质量门禁通过且可回滚 | 关闭feature flag | 跨仓语义漂移 | 是 | P1 |
| B-009 | P2 | DataAnalysis/Oagnet/sql-translator | 角色化N-best | 前序阶段 | 按规格实现并保留旧链 | 单测+Shadow+回放 | 质量门禁通过且可回滚 | 关闭feature flag | 跨仓语义漂移 | 是 | P2 |
| B-010 | P2 | DataAnalysis/Oagnet/sql-translator | Typed Plan Shadow | 前序阶段 | 按规格实现并保留旧链 | 单测+Shadow+回放 | 质量门禁通过且可回滚 | 关闭feature flag | 跨仓语义漂移 | 是 | P2 |
| B-011 | P2 | DataAnalysis/Oagnet/sql-translator | Legacy Adapter对比 | 前序阶段 | 按规格实现并保留旧链 | 单测+Shadow+回放 | 质量门禁通过且可回滚 | 关闭feature flag | 跨仓语义漂移 | 是 | P2 |
| B-012 | P3 | DataAnalysis/Oagnet/sql-translator | ASL扩展 | 前序阶段 | 按规格实现并保留旧链 | 单测+Shadow+回放 | 质量门禁通过且可回滚 | 关闭feature flag | 跨仓语义漂移 | 是 | P3 |
| B-013 | P3 | DataAnalysis/Oagnet/sql-translator | SQL粒度安全 | 前序阶段 | 按规格实现并保留旧链 | 单测+Shadow+回放 | 质量门禁通过且可回滚 | 关闭feature flag | 跨仓语义漂移 | 是 | P3 |
| B-014 | P3 | DataAnalysis/Oagnet/sql-translator | ResultContract | 前序阶段 | 按规格实现并保留旧链 | 单测+Shadow+回放 | 质量门禁通过且可回滚 | 关闭feature flag | 跨仓语义漂移 | 是 | P3 |
| B-015 | P3 | DataAnalysis/Oagnet/sql-translator | 内部修复循环 | 前序阶段 | 按规格实现并保留旧链 | 单测+Shadow+回放 | 质量门禁通过且可回滚 | 关闭feature flag | 跨仓语义漂移 | 是 | P3 |
| B-016 | P4 | DataAnalysis/Oagnet/sql-translator | Topic Tree | 前序阶段 | 按规格实现并保留旧链 | 单测+Shadow+回放 | 质量门禁通过且可回滚 | 关闭feature flag | 跨仓语义漂移 | 是 | P4 |
| B-017 | P4 | DataAnalysis/Oagnet/sql-translator | 历史恢复 | 前序阶段 | 按规格实现并保留旧链 | 单测+Shadow+回放 | 质量门禁通过且可回滚 | 关闭feature flag | 跨仓语义漂移 | 是 | P4 |
| B-018 | P4 | DataAnalysis/Oagnet/sql-translator | 动态上下文 | 前序阶段 | 按规格实现并保留旧链 | 单测+Shadow+回放 | 质量门禁通过且可回滚 | 关闭feature flag | 跨仓语义漂移 | 是 | P4 |
| B-019 | P4 | DataAnalysis/Oagnet/sql-translator | 高级会话状态 | 前序阶段 | 按规格实现并保留旧链 | 单测+Shadow+回放 | 质量门禁通过且可回滚 | 关闭feature flag | 跨仓语义漂移 | 是 | P4 |
| B-020 | P5 | DataAnalysis/Oagnet/sql-translator | 趋势/构成 | 前序阶段 | 按规格实现并保留旧链 | 单测+Shadow+回放 | 质量门禁通过且可回滚 | 关闭feature flag | 跨仓语义漂移 | 是 | P5 |
| B-021 | P5 | DataAnalysis/Oagnet/sql-translator | 异常/贡献 | 前序阶段 | 按规格实现并保留旧链 | 单测+Shadow+回放 | 质量门禁通过且可回滚 | 关闭feature flag | 跨仓语义漂移 | 是 | P5 |
| B-022 | P5 | DataAnalysis/Oagnet/sql-translator | 归因/预测 | 前序阶段 | 按规格实现并保留旧链 | 单测+Shadow+回放 | 质量门禁通过且可回滚 | 关闭feature flag | 跨仓语义漂移 | 是 | P5 |
| B-023 | P5 | DataAnalysis/Oagnet/sql-translator | 血缘/质量 | 前序阶段 | 按规格实现并保留旧链 | 单测+Shadow+回放 | 质量门禁通过且可回滚 | 关闭feature flag | 跨仓语义漂移 | 是 | P5 |
| B-024 | P6 | DataAnalysis/Oagnet/sql-translator | 灰度门禁 | 前序阶段 | 按规格实现并保留旧链 | 单测+Shadow+回放 | 质量门禁通过且可回滚 | 关闭feature flag | 跨仓语义漂移 | 是 | P6 |
| B-025 | P6 | DataAnalysis/Oagnet/sql-translator | 性能成本 | 前序阶段 | 按规格实现并保留旧链 | 单测+Shadow+回放 | 质量门禁通过且可回滚 | 关闭feature flag | 跨仓语义漂移 | 是 | P6 |
| B-026 | P6 | DataAnalysis/Oagnet/sql-translator | 安全隔离 | 前序阶段 | 按规格实现并保留旧链 | 单测+Shadow+回放 | 质量门禁通过且可回滚 | 关闭feature flag | 跨仓语义漂移 | 是 | P6 |
| B-027 | P6 | DataAnalysis/Oagnet/sql-translator | 旧链退役 | 前序阶段 | 按规格实现并保留旧链 | 单测+Shadow+回放 | 质量门禁通过且可回滚 | 关闭feature flag | 跨仓语义漂移 | 是 | P6 |
