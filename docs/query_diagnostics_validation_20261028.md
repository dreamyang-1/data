# 查询诊断日志隔离回归（2026-10-28）

新增日志的专项测试 24 项通过；扩大范围基线 155 通过、9 失败，修改后 158 通过、相同 9 失败，无新增失败或收集错误。按发布约定，存在测试失败时暂不部署。

隔离环境从正常请求入口执行真实问题：销售总额案例的日志依次显示结构化维度为空、ASL 维度为空、SQL 无 GROUP BY 但有 HAVING、返回 1 行；另一次同问题返回 32 行。公司明细的一次结果为 0 行，日志记录了默认时间范围被绑定到经销商首次订单日期；该日期此前查得为 2022 年，不在默认窗口内。这些是独立的业务问题，本次只补日志，不修改查询。

历史问题在相同语义模型和业务域下使用独立会话、有界并发执行；部署前基线使用同日已有线上回归。以下状态和行数不能代替完整业务核验。

| 问题标识（原文及授权范围见回归清单） | 线上基线 | 隔离候选 | 核验状态 |
| --- | --- | --- | --- |
| two_brands_manufacturers_multirow | COMPLETED；行数 [7] | COMPLETED；行数 [7] | 已执行；业务内容未逐项核验 |
| two_brands_tier_two_hospitals | COMPLETED；行数 [2] | COMPLETED；行数 [2] | 已执行；业务内容未逐项核验 |
| huashan_sales_products | COMPLETED；行数 [1, 926] | COMPLETED；行数 [1, 926] | 已执行；业务内容未逐项核验 |
| shanghai_three_dealers_products-turn1 | COMPLETED；行数 [3] | COMPLETED；行数 [3] | 已执行；业务内容未逐项核验 |
| shanghai_three_dealers_products-turn2 | COMPLETED；行数 [396] | COMPLETED；行数 [396] | 未完全核验：需确认是否按三家分别返回 |
| company_top5 | COMPLETED；行数 [89, 89, 5, 5] | COMPLETED；行数 [89, 89, 5, 5] | 已执行；业务内容未逐项核验 |
| shanghai_coverage | COMPLETED；行数 [589, 1, 589] | COMPLETED；行数 [589, 1, 589] | 已执行；业务内容未逐项核验 |
| hospital_sales_products | COMPLETED；行数 [1, 5] | COMPLETED；行数 [1, 5] | 已执行；业务内容未逐项核验 |
| semantic_entities | COMPLETED；行数 [] | COMPLETED；行数 [] | 已执行；业务内容未逐项核验 |
| orthopedics | COMPLETED；行数 [1] | COMPLETED；行数 [1] | 已执行；业务内容未逐项核验 |
| longest_cooperation | NEEDS_CLARIFICATION；行数 [] | COMPLETED；行数 [1] | 执行不稳定：基线需补充，候选完成；单独拦停案例日志已核验 |
| cooperation_over_three_months | COMPLETED；行数 [1000] | COMPLETED；行数 [2154] | 部分：基线或候选结果受1000条上限/对象口径影响 |
| dealer_duration_exact | COMPLETED；行数 [1000] | COMPLETED；行数 [1000] | 部分：基线或候选结果受1000条上限/对象口径影响 |
| dealer_duration_recent | COMPLETED；行数 [1000] | COMPLETED；行数 [1000] | 部分：基线或候选结果受1000条上限/对象口径影响 |
| supplier_first_cooperation | COMPLETED；行数 [1000] | COMPLETED；行数 [1000] | 部分：基线或候选结果受1000条上限/对象口径影响 |
| shanghai_dealer_sales_total_threshold | COMPLETED；行数 [1] | COMPLETED；行数 [32] | 未完全通过：独立复测出现未分组及正确分组两种结果 |
| shanghai_pharma_company_details | NEEDS_CLARIFICATION；行数 [] | COMPLETED；行数 [0] | 未通过：基线需补充，候选返回0行，默认日期范围与首次订单日期不符 |

原始上下文或授权范围缺失的其他条目仍列在回归清单的 `unresolved_context`，未计为通过。结果和日志仅保存在隔离验证环境，不提交业务数据。

## 扩大测试中修改前后均失败的用例

以下为用例名称；失败不等同于本次日志改动引入了对应业务问题。

2026-10-28 后续核对：7 项测试请求缺少分析工具选择。补齐注册工具后，6 项原断言通过；第 7 项原本要求适配器删除 ASL 中的时间条件，与当前禁止事后改写 ASL 的合同冲突，改为检查结构化结果向下传递时不增添时间范围。上游结构化绑定测试 51 项通过，其中已有无时间范围及显式时间范围的覆盖；这不代表所有异常上游响应都会被下游重新校验。

另两项是过时断言：未绑定的语义提及按现行合同记录而不创建过滤条件；ASL 已报告的指标歧义应停止而不能被适配器忽略。该记录并不证明忽略提及后业务结果完整。上述调整后，本次日志相关测试范围 167 项通过，原有业务问题及历史回归的未验证项仍按本报告标识。

- `tests/test_http_adapters.py::test_semantic_entity_mention_rejects_missing_source_binding_proof`
- `tests/test_http_adapters.py::test_ranked_partner_sales_record_range_uses_transaction_time_scope`
- `tests/test_http_adapters.py::test_dealer_recommendation_uses_relationship_aware_retrieval_scope`
- `tests/test_http_adapters.py::test_precomputed_metric_window_cannot_add_fact_time_scope`
- `tests/test_http_adapters.py::test_comparison_query_requires_grouped_asl_and_adds_deterministic_hint`
- `tests/test_http_adapters.py::test_unbounded_entity_sort_requires_metric_order_without_limit`
- `tests/test_http_adapters.py::test_unbounded_entity_sort_rejects_hidden_limit_before_sql`
- `tests/test_http_adapters.py::test_comparison_aggregate_without_group_dimension_fails_before_sql`
- `tests/test_http_adapters.py::test_confirmed_metric_ignores_repeated_asl_metric_ambiguity`
