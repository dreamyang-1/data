# V2 Context → V1 Execution Contract Validation

**V2_CONTEXT_V1_EXECUTION_CONTRACT_COMPLETE**

本轮只修正 Bridge 输入合同和 `completed_question` 展示，没有修改 V1/V2 业务语义、Prompt、Oagnet、SQL Translator、Redis schema、Session 架构或前端。

## 同问题 V1 / Bridge 诊断

固定问题为“查询去年江苏省订单笔数”，Scope 为 semantic model 81、business domains `[205]`。

| 合同项 | 原纯 V1 | 修正前 Bridge |
|---|---|---|
| question / rewritten_question | 原始问题 / 原始问题 | `查询2025年、筛选条件为省份名称等于江苏省的订单笔数。` |
| semantic_model_id | 81 | 81 |
| business_domain_ids | `[205]` | `[205]` |
| resolved_business_domain_ids | `[205]` | `[]` |
| database_id / knowledge_base_names | `null / []` | `null / []` |
| dependency_constraints / dataset_id | `[] / null` | `[] / null` |
| metric | 订单笔数 | 订单笔数 |
| dimensions | `[]` | `[省份]` |
| filter | 省份名称 EQ 江苏省 | 商品名称 EQ `2025年、筛选条件为省份名称等于江苏省` |
| internal context-resolved flag | false | true（进入 V1 execution 前） |
| 下游结果 | `ASL_AMBIGUOUS` | `DEPENDENCY_CONTRACT_REJECTED / ASL_GROUPING_DIMENSION_MISSING` |

`DEPENDENCY_CONTRACT_REJECTED = BRIDGE_ONLY_FAILS`。修正前 Bridge 把内部合同措辞重新交给 V1 自然语言入口，V1 将它误解析为商品过滤并增加省份分组；纯 V1 没有产生这一错误合同。纯 V1 同次诊断仍因独立的 `ASL_AMBIGUOUS` 失败，本轮没有修改该 V1 问题。

## 最小修正

- 自包含 `NEW_TASK` 在 Bridge 层把原始问题作为 execution question，保持 V1 已有解析合同，不按 Query Shape 特判。
- `FOLLOWUP/MODIFY` 仍必须由 V2 TaskState 形成完整问题；精确实体值使用自然措辞，例如“查询2026年江苏省订单笔数。”。
- 进入 V1 前继续强制 `history=[]`、`question=completed_question` 和 internal context-resolved flag；其他请求字段逐字段原样保留。
- 原接口 `analysis_process` 的“补全后的完整问题”直接使用已送入 V1 的同一字符串。

## 8088 真实验收

当前 8088 进程以 `V2_CONTEXT_V1_EXECUTION` 启动，Oagnet 与 SQL Translator 进程未重启。

| 轮次 | API 结果 | completed_question / TaskState | 原执行链证据 |
|---|---|---|---|
| 首问 | HTTP 200 / COMPLETED | 原问题直接交给 V1；Task v1 为 2025、江苏省、订单笔数 | `SEMANTIC_METRIC_RESOLUTION source_ref=oagnet-asl`；`QUERY_RESULT source_ref=data-source:58`；一致性 MySQL 查询成功 |
| “换今年” | HTTP 200 / COMPLETED | `查询2026年江苏省订单笔数。`；Task v2 仅替换时间，地区和指标保留 | 新的 Oagnet/SQL/DB 查询结果证据；没有复用首轮结果 |

Redis 只读复核：conversation state version=2；Task active version=2；m1/m2 均为 `V1_EXECUTION_RESPONSE_SAVED` 且 `v1_execution_called=true`。没有伪造 TaskState。SQL 写入、Catalog 写入、配置修改均为 0。

## 验证

- 专项及相关离线：91 passed。
- 隔离全量：3630 passed / 91 existing failed / 0 collection errors。
- 与既有 91 项失败 node set 对比：old-pass → new-fail = 0。
- 开发目录另外两项 `test_api.py` 失败来自本轮开始前的 `app/api.py` 标题文本改动；该文件不在本轮清单，也未被覆盖。

当前结论只覆盖 V2 Context → completed question → 原 V1 execution 的目标链，不扩大为 cutover、canary 或 V1 replacement。
