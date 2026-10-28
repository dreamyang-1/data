# 异常解释上线验证（2026-10-28）

解释模块去掉提示词、模型请求和输出处理中的长度限制；有直接证据时说明具体漏提或放错的位置，不将字段匹配失败误说成用户漏填。敏感信息脱敏与证据编号校验保留。

专项测试 40 项通过；真实模型的构造证据测试中 15 项明确遗漏均被列出。真实历史问题“合作时间最长的销售商是哪家?”在最终线上版本的一次拦停中，解释指出合作时长放在展示字段、没有作为指标提取，无法排序；原状态与追问保留。该问题另一次线上测试成功，提取结果仍不稳定；本次不修改提取逻辑。

第一次最终回归期间服务再次停止启动，部分请求连接被拒；恢复后核对本次四个文件的哈希并重新执行全部可追溯问题。该次服务停止的触发来源未查明，不把被中断请求算作通过。

部署前后同一天的请求按独立会话、有界并发执行。下表的状态及返回行数仅是证据，不代表业务内容已全部核验；多轮问题逐轮对照。

| 原问题标识（原文和授权范围见回归清单） | 部署前 | 最终线上重跑 | 业务核验状态 |
| --- | --- | --- | --- |
| two_brands_manufacturers_multirow | COMPLETED；行数 [12] | COMPLETED；行数 [7] | 返回行数由12变7；分组及业务口径尚待核验 |
| two_brands_tier_two_hospitals | COMPLETED；行数 [2] | COMPLETED；行数 [2] | 已执行；业务内容未逐项核验 |
| huashan_sales_products | COMPLETED；行数 [1, 926] | COMPLETED；行数 [1, 926] | 已执行；业务内容未逐项核验 |
| shanghai_three_dealers_products-turn1 | COMPLETED；行数 [3] | COMPLETED；行数 [3] | 已执行；业务内容未逐项核验 |
| shanghai_three_dealers_products-turn2 | COMPLETED；行数 [0] | COMPLETED；行数 [396] | 未完全通过：需核验是否按三家分别返回；部署前已有遗漏 |
| company_top5 | COMPLETED；行数 [89, 89, 5, 5] | COMPLETED；行数 [89, 89, 5, 5] | 已执行；业务内容未逐项核验 |
| shanghai_coverage | COMPLETED；行数 [589, 1, 589] | COMPLETED；行数 [589, 1, 589] | 已执行；业务内容未逐项核验 |
| hospital_sales_products | COMPLETED；行数 [1, 5] | COMPLETED；行数 [1, 5] | 已执行；业务内容未逐项核验 |
| semantic_entities | COMPLETED；行数 [] | COMPLETED；行数 [] | 已执行；业务内容未逐项核验 |
| orthopedics | COMPLETED；行数 [1] | COMPLETED；行数 [1] | 已执行；业务内容未逐项核验 |
| longest_cooperation | COMPLETED；行数 [1] | NEEDS_CLARIFICATION；行数 [] | 解释通过；提取有时放错位置，原问题执行不稳定 |
| cooperation_over_three_months | COMPLETED；行数 [1000] | COMPLETED；行数 [1000] | 部分：触及1000条上限；完整性及供应商口径待核验 |
| dealer_duration_exact | COMPLETED；行数 [1000] | COMPLETED；行数 [1000] | 部分：触及1000条上限；完整性及供应商口径待核验 |
| dealer_duration_recent | COMPLETED；行数 [1000] | COMPLETED；行数 [1000] | 部分：触及1000条上限；完整性及供应商口径待核验 |
| supplier_first_cooperation | COMPLETED；行数 [1000] | COMPLETED；行数 [1000] | 部分：触及1000条上限；完整性及供应商口径待核验 |
| shanghai_dealer_sales_total_threshold | COMPLETED；行数 [32] | COMPLETED；行数 [1] | 未通过：缺少经销商分组，返回1家但金额是总和；连续复测分别返回1家和32家，原因及影响需进一步处理 |
| shanghai_pharma_company_details | NEEDS_CLARIFICATION；行数 [] | NEEDS_CLARIFICATION；行数 [] | 前后均需补充；公司实体识别不稳定，待核验 |

缺少完整原问题或授权范围的条目仍在回归清单的 `unresolved_context`，未计为通过。日志与业务结果保留在服务器临时验证目录，不纳入 Git。
