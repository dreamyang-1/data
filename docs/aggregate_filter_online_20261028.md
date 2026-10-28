# 汇总指标筛选上线验证（2026-10-28）

服务器现有代码与本次修复合并，保留 scoped_dimension 兼容；重启 ASL 服务并通过健康检查。合并版本专项测试 80 项通过。此前完整离线套件有 39 项既有失败，与修改前相同；本次未宣称这些失败已解决。

原问题“统计上海市的含税销售总额大于100万的经销商”在线上生成 `HAVING SUM(sales_order.amount_with_tax) > 1000000`，不再按单笔订单金额过滤；上海筛选保留，返回 32 条、附件下载成功。问题未指定时间，2026-10-28 重跑使用系统默认窗口 2025-10-28 至 2026-10-28；来源数据水位仅至 2025-12-30。

下表依据同日部署前后真实请求结果；记录行数和请求状态不等于已证明业务结果完全正确。多轮问题按顺序执行，不同问题使用独立会话、有界并发。

| 原问题标识（原文及范围见回归清单） | 部署前 | 部署后 | 验证状态 |
| --- | --- | --- | --- |
| two_brands_manufacturers_multirow | COMPLETED；行数 [12] | COMPLETED；行数 [12] | 已执行；业务内容未逐项核验 |
| two_brands_tier_two_hospitals | COMPLETED；行数 [2] | COMPLETED；行数 [2] | 已执行；业务内容未逐项核验 |
| huashan_sales_products | COMPLETED；行数 [1, 926] | COMPLETED；行数 [1, 926] | 已执行；业务内容未逐项核验 |
| shanghai_three_dealers_products-turn1 | COMPLETED；行数 [3] | COMPLETED；行数 [3] | 已执行；业务内容未逐项核验 |
| shanghai_three_dealers_products-turn2 | COMPLETED；行数 [0] | COMPLETED；行数 [1] | 未通过：未按三家分别返回产品，部署前已存在 |
| company_top5 | COMPLETED；行数 [89, 89, 5, 5] | COMPLETED；行数 [89, 89, 5, 5] | 已执行；业务内容未逐项核验 |
| shanghai_coverage | COMPLETED；行数 [589, 1, 589] | COMPLETED；行数 [589, 1, 589] | 已执行；业务内容未逐项核验 |
| hospital_sales_products | COMPLETED；行数 [1, 5] | COMPLETED；行数 [1, 5] | 已执行；业务内容未逐项核验 |
| semantic_entities | COMPLETED；行数 [] | COMPLETED；行数 [] | 已执行；业务内容未逐项核验 |
| orthopedics | COMPLETED；行数 [1] | COMPLETED；行数 [1] | 已执行；业务内容未逐项核验 |
| longest_cooperation | 需补充；行数 [] | 需补充；行数 [] | 未通过：前后均要求补充 |
| cooperation_over_three_months | COMPLETED；行数 [1000] | COMPLETED；行数 [1000] | 部分：结果触及1000条上限 |
| dealer_duration_exact | COMPLETED；行数 [1000] | COMPLETED；行数 [1000] | 部分：结果触及1000条上限 |
| dealer_duration_recent | COMPLETED；行数 [1000] | COMPLETED；行数 [1000] | 部分：结果触及1000条上限 |
| supplier_first_cooperation | COMPLETED；行数 [1000] | COMPLETED；行数 [1000] | 部分：结果触及1000条上限；供应商口径仍需核验 |
| shanghai_dealer_sales_total_threshold | COMPLETED；行数 [5] | COMPLETED；行数 [32] | 通过：汇总后筛选；受数据水位限制 |
| shanghai_pharma_company_details | COMPLETED；行数 [1] | COMPLETED；行数 [1000] | 部分：两次线上运行分别返回档案及1000条订单明细，查询粒度不稳定，后者受上限限制 |

其他原问题若缺少原始上下文、实体 ID 或授权范围，仍列于回归清单的 `unresolved_context`，未计为通过。部署前后请求和测试详细证据保留在服务器临时验证目录；日志及业务数据不提交仓库。
