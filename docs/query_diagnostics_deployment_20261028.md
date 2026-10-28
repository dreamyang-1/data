# 查询诊断日志部署验证（2026-10-28）

根据用户明确部署要求，将日志功能三方合并到服务器现有文件，部署前备份并核对文件哈希；重启数据智能体后健康接口就绪。其他两个服务保持运行。

部署版本：功能提交 `0a2a84c`，测试契约更新 `160aedd`。部署前专项相关范围 167 项通过，上游结构化绑定 51 项通过。

新日志已观察到结构化提取、ASL 接收、SQL 翻译与执行、生成失败和结构化拦停事件；日志可按请求标识关联。统计仅证明观察到事件，不代表所有故障都有足够证据。

以下原问题在原模型及业务域下以独立会话、有界并发 2 执行；状态和行数不等于完整业务验收，测试结果未逐条核对全部内容。原文和历史上下文见业务回归清单；无法取得上下文的条目仍未验证。

| 问题标识 | 线上状态 |
| --- | --- |
| company_top5 | COMPLETED |
| cooperation_over_three_months | COMPLETED |
| dealer_duration_exact | COMPLETED |
| dealer_duration_recent | COMPLETED |
| hospital_sales_products | COMPLETED |
| huashan_sales_products | COMPLETED |
| longest_cooperation | COMPLETED |
| orthopedics | COMPLETED |
| semantic_entities | COMPLETED |
| shanghai_coverage | COMPLETED |
| shanghai_dealer_sales_total_threshold | NEEDS_CLARIFICATION |
| shanghai_pharma_company_details | COMPLETED |
| shanghai_three_dealers_products-turn1 | NEEDS_CLARIFICATION |
| shanghai_three_dealers_products-turn2 | SAFE_FALLBACK |
| supplier_first_cooperation | COMPLETED |
| two_brands_manufacturers_multirow | COMPLETED |
| two_brands_tier_two_hospitals | COMPLETED |

## 未解决事项

- 上海三家经销商多轮问题：第一轮“上海”未匹配到所选字段的标准值，返回补充询问；第二轮没有第一轮的三家公司，未能继续。部署前的隔离回归曾完成，变化是否由模型或目录结果波动引起尚未确认。
- 上海销售总额问题本次同样因省份字段的“上海”标准值未匹配而停止，未进入 SQL；此前隔离回归曾出现漏分组或正确分组两种结果。
- 公司明细本次返回空结果，业务完整性未通过；此前一次隔离日志显示默认时间范围绑定首次订单日期，不能据此推断本次必然相同。
- 四个合作时长相关结果达到返回行数上限，完整性未逐项验证；两品牌厂家案例与部署前隔离回归的行数不同，原因未确认。不得将状态为 COMPLETED 等同于业务结果全部正确。
- 对其余已执行问题，单位、排序、附件、输出内容等未逐条复核；缺失上下文的问题未运行，不能宣称所有历史回归通过。
