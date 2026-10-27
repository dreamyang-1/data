# 汇总指标筛选修复验证记录

- 原问题：统计上海市的含税销售总额大于100万的经销商；模型 130，业务域为空（模型范围）。原问题未指定时间，执行时仍使用系统原有默认时间范围。
- 修改前：向量标准化将过滤字段“含税销售总额”改成“含税金额”，SQL 在 WHERE 对单笔金额筛选；复现返回 5 条。
- 隔离候选版本：上海条件仍在 WHERE，指标条件为 `HAVING SUM(sales_order.amount_with_tax) > 1000000`；返回 32 条，按总额降序；XLSX 下载成功。数据水位仅至 2025-12-30，默认时间范围尚未完整覆盖。线上版本未部署，不能视为线上验收。
- 离线专项：78 项通过。完整离线测试修改前 2000 通过、39 失败；修改后 2004 通过、39 失败；无新增失败、无测试收集错误。既有失败未在本次改动中修复，按约定停止发布。
- 历史问题用独立会话和有界并发分别在修改前及隔离候选版本运行，保留多轮顺序。下表的“执行完成”只表示请求返回，不等于业务内容全部通过。

| 问题标识 | 前后状态及尚存限制 |
| --- | --- |
| huashan_sales_products | 两个任务均执行完成；数据水位未覆盖全年。 |
| shanghai_three_dealers_products | 两轮均执行完成；原有医院地域归属口径仍需核验。 |
| company_top5 | 四个任务均执行完成；数据水位未覆盖全年。 |
| shanghai_coverage | 三个任务均执行完成；结果业务口径尚需进一步核验。 |
| hospital_sales_products | 两个任务均执行完成；数据水位未覆盖完整时间范围。 |
| semantic_entities | 执行完成并返回语义实体。 |
| orthopedics | 执行完成；数据水位未覆盖完整时间范围。 |
| longest_cooperation | 前后均需补充确认：合作时长被提取为展示字段而非指标。 |
| cooperation_over_three_months | 执行完成，但结果触及 1000 条上限，未证明完整。 |
| dealer_duration_exact | 执行完成，但结果触及 1000 条上限，未证明完整。 |
| dealer_duration_recent | 执行完成，但结果触及 1000 条上限，未证明完整。 |
| supplier_first_cooperation | 执行完成；“供应商”映射到经销商的业务含义及结果完整性尚需核验。 |
| shanghai_dealer_sales_total_threshold | 隔离候选版本中 SQL 汇总筛选、结果排序及附件下载通过；数据水位和线上部署仍有限制。 |

原始上下文和授权范围缺失的其他历史问题仍见 `tests/business_question_regression.json` 的 `unresolved_context`，没有将其视为已验证。
