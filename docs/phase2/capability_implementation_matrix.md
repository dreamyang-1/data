# 能力实现矩阵

| 能力 | DataAnalysis | Oagnet | SQL Translator | 结论 |
|---|---|---|---|---|
| 新问题/追问判断 | 已实现，多规则与模型协调 | 不负责 | 不负责 | PARTIAL，回归仍有状态转换失败 |
| 槽位继承/替换 | 已实现启发式合并 | 调用方契约回填 | 不负责 | PARTIAL，缺少 typed reducer |
| 指标规范化 | 生成必需指标契约 | 类型化召回+规范化 | 目录验证 | IMPLEMENTED |
| 实体值规范化 | 候选展示/约束 | entity value 向量召回 | 过滤字段校验 | PARTIAL，缺少拒绝证据 |
| 多指标 | 调用方契约支持 | ASL metrics[] | SQL 聚合支持 | IMPLEMENTED，结果证明不足 |
| 时间粒度 | 解析与策略 | ASL dimension granularity | DATE粒度转换 | PARTIAL，历史多例缺投影 |
| 排序/Top N | 解析 | ASL sort/limit | ORDER/LIMIT | PARTIAL，数据集追问路由易错 |
| 比较分析 | analysis_operator | 非一等ASL | 特殊分析契约 | PARTIAL |
| 关系路径 | 前置校验 | 最短路径补全 | 目录Join | IMPLEMENTED，基数治理不足 |
| 1:N安全 | 轻量校验 | 无 | 发现后拒绝 | PARTIAL，无预聚合 |
| ResultContract | Dataset/分析契约 | envelope | 六种操作校验 | PARTIAL |
| 完整可观测性 | 事件与trace摘要 | 缺候选拒绝trace | 执行证据 | NOT_COMPLETE |
| 多租户状态隔离 | key设计存在 | 请求scope | modelId/source | UNKNOWN，缺并发验证 |

标签依据均可追溯到主报告和 `architecture_evidence_map.json`。矩阵不把“代码存在”当成“业务正确性已证明”。

