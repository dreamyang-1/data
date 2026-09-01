# DETAIL_QUERY ASL Projection & Error Classification Fix Report

## 1. 修复范围

本次修改只处理 Intent 到 ASL 的结构契约、DETAIL/ENTITY/RELATION 查询投影、过滤条件、排序、默认时间范围和错误分类问题。

未修改多轮会话、Memory、Semantic Layer 数据定义、数据库表结构、前端和无关 Skill，也没有针对“经销商”写业务特判。

## 2. 根因

原链路中意图识别和 ASL 生成之间主要依赖自然语言提示，没有机器可校验的查询形状契约，导致：

1. `DETAIL_QUERY` 已明确不需要指标，但 ASL 仍可能生成指标，或在投影缺失时被笼统归类为 `ASL_METRIC_SELECTION_INVALID`。
2. 用户要求的“经销商名称”等语义字段，没有被确定性映射到本次召回元数据中的 logical dimension / physical field。
3. 强制筛选条件只靠提示保留，缺少字段、值和正负极性的逐项校验。
4. 默认最近一年时间范围被错误应用到产品—经销商关系名单，触发无意义的交易时间锚点追问。
5. 关系名单被规范化为“查询明细”后，集合语义被误判为事实明细语义，SQL 未使用 `DISTINCT`，产生重复名称。
6. DataAnalysis_Agent 对 Oagnet 已确认的 logical dimension 缺少可审计的语义投影映射，出现二次误拒绝。

## 3. 通用 Intent-ASL Contract

新增调用方拥有的 `IntentASLContract`，跨 DataAnalysis_Agent 和 Oagnet 传递。Contract 只包含业务语义标签，不包含猜测出来的表名或字段名。

核心字段：

- `intent`
- `query_object`
- `metric_required`
- `required_metrics`
- `required_metric_codes`
- `required_projections`
- `projection_mode`: `DISTINCT | ROWS`
- `filters`
- `negative_filters`
- `sorting`
- `time_dimension_required`
- `time_policy`: `REQUIRED | OPTIONAL | FORBIDDEN`

执行规则：

1. DETAIL 查询删除模型额外生成的指标。
2. 必需投影仅允许从本次召回的实体属性或 logical dimension 元数据中解析。
3. 过滤器必须同时匹配语义字段、值、运算符和正负极性；物理字段必须来自本次召回元数据。
4. 只有唯一映射时才执行一次确定性修复；无法唯一确定时失败并返回候选信息，禁止按字段名猜测。
5. DETAIL 结果只保留 Contract 要求的投影，过滤字段不会被错误显示为结果列。
6. 默认时间范围对无时间语义的关系名单使用 `FORBIDDEN`，显式时间与趋势查询仍按各自 Contract 处理。
7. 关系集合使用 `DISTINCT`，订单、交易、事件等用户明确要求的事实明细使用 `ROWS`。
8. Oagnet 返回投影解析和修复审计，DataAnalysis_Agent 用该审计验证 logical dimension，不再用字符串规则误拒绝合法投影。

## 4. 精准错误分类

| 错误码 | 含义 |
| --- | --- |
| `ASL_DETAIL_PROJECTION_MISSING` | DETAIL 查询缺少必需投影，或语义投影不能从召回元数据唯一确定 |
| `ASL_METRIC_SELECTION_INVALID` | 指标型查询缺少或选择了错误的治理指标 |
| `ASL_FILTER_INVALID` | 必需正向/排除筛选缺失、极性错误或字段不能唯一映射 |
| `ASL_SORT_INVALID` | 排名查询缺少 Contract 指定的排序 |
| `ASL_TIME_DIMENSION_MISSING` | 趋势查询缺少时间上下文或时间粒度 |
| `ASL_TIME_SCOPE_INVALID` | Contract 禁止时间范围但 ASL 仍带隐式默认时间 |

错误码优先读取结构化 `ASLValidationError.code`；不再用宽泛的字符串中是否含有 `metric` 来覆盖真实错误。

## 5. Contract Regression Matrix

每个 Case 均验证 `intent`、`query_object`、`metric_required`、`metric`、`projection`、完整 `filters`、`negative_filters`、`sorting`、ASL Contract 定义和 validation result。

| Case | 问题类型 | 关键 Contract |
| --- | --- | --- |
| 1 | A 产品合作经销商 | DETAIL / 经销商 / 经销商名称 / 商品名称过滤 / DISTINCT |
| 2 | A 产品合作医院 | DETAIL / 医院 / 医院名称 / 商品名称过滤 / DISTINCT |
| 3 | A 厂家产品 | DETAIL / 商品 / 商品名称 / 厂家名称过滤 / DISTINCT |
| 4 | A 产品厂家 | DETAIL / 厂家 / 厂家名称 / 商品名称过滤 / DISTINCT |
| 5 | 上海经销商 | DETAIL / 经销商 / 经销商名称 / 地区=上海市 / DISTINCT |
| 6 | A 产品规格 | DETAIL / 商品 / 商品规格 / 商品名称过滤 / ROWS |
| 7 | A 产品销售额 | METRIC / 销售额必需 / 商品名称过滤 |
| 8 | A 产品销售量 | METRIC / 销售量必需 / 商品名称过滤 |
| 9 | A 产品销售额最高的经销商 | METRIC / 经销商 / 销售额 / DESC / LIMIT 1 |
| 10 | 上海销售规模最大的医院 | METRIC / 医院 / 整体业务规模 / 地区过滤 / DESC / LIMIT 1 |
| 11 | A 产品按月销售趋势 | TREND / 销售额 / 商品名称过滤 / time_policy=REQUIRED |
| 12 | 上海 A 产品经销商 | DETAIL / 经销商名称 / 地区和商品双过滤 / DISTINCT |
| 13 | 排除 B 厂家的 A 产品经销商 | DETAIL / 商品正向过滤 / 厂家 NE 排除过滤 / DISTINCT |

产品范围规范化也采用通用规则：作为语法性类别后缀的“产品/商品”不会被写入产品名称值。例如“空心纤维血液透析器产品”规范为 `商品名称 = 空心纤维血液透析器`。

## 6. 修改文件

DataAnalysis_Agent：

- `app/services/intent_asl_contract.py`
- `app/services/relationship_projection.py`
- `app/intent/classifier.py`
- `app/adapters/http.py`
- `app/services/orchestrator.py`
- `tests/test_intent_asl_contract_matrix.py`
- `tests/test_http_adapters.py`

Oagnet：

- `asl_contract.py`
- `agent.py`
- `api.py`
- `tests/test_intent_asl_contract.py`
- `tests/test_api_error_codes.py`

## 7. 测试结果

专项回归：

- Contract Matrix：13 个业务 Case 全部通过，并覆盖 canonical rewrite 集合语义回归
- DataAnalysis_Agent 本次 P0 专项回归：111 passed
- Oagnet Contract、元数据消歧与错误分类专项回归：41 passed

全量回归：

- Oagnet：201 passed，1 个既有 legacy time dimension 归一化用例失败；与本次 Contract 修改无关。
- DataAnalysis_Agent：1144 passed，26 个既有澄清流、OpenAPI、会话事件和 mock report 用例失败；本次专项相关用例全部通过，未增加失败项。

## 8. 真实端到端验收

请求：

> 查询空心纤维血液透析器产品合作的经销商名单。

语义模型：`81`

最终状态：

- `status=COMPLETED`
- `intent=DETAIL_QUERY`
- `metric_required=false`
- 投影：经销商 logical dimension
- 过滤：`product.product_name = '空心纤维血液透析器'`
- 时间：`time_context=null`
- 输出模式：`DISTINCT`
- 数据质量：`PASS`
- 返回：22 个唯一经销商

实际 SQL：

```sql
SELECT DISTINCT dealer.dealer_name AS `经销商`
FROM dealer
LEFT JOIN dealer_product_relation
  ON dealer.dealer_code = dealer_product_relation.dealer_code
LEFT JOIN product
  ON dealer_product_relation.product_code = product.product_code
WHERE product.product_name = '空心纤维血液透析器'
LIMIT 10000
```

该 SQL 使用语义层已注册的关系路径和字段，未复制历史 SQL，也没有根据字段名猜 Join。

## 9. P0 Global Query Regression 增量修复

本阶段继续解决了单个 Case 背后的通用结构问题，而不是为“经销商”写特判：

1. 当前轮显式约束在上下文合并前形成 `CurrentTurnFacts`，在发送 Oagnet 前与 `IntentASLContract` 做完整性对账；指标、查询对象、投影、正负筛选、排序和 Top N 任一丢失都会 fail closed。
2. 独立新问题使用原始用户问题执行；只有真正的上下文追问、纠正或历史任务才使用 canonical query，避免渲染后的摘要替换用户原话。
3. 复合问题统一抽取“产品 → 业务对象”、地区、厂家排除和排序要求；单产品作为筛选时不再错误地同时成为分组维度。
4. DataAnalysis_Agent 和 Oagnet 均校验筛选极性，`NE/!=/NOT IN` 不允许在 ASL 或 SQL 中静默退化为等值条件。
5. `metric_required=false + required_metric_codes=[]` 被视为权威的无指标 Contract；Oagnet 不再从检索扩展文本中反推销售额指标。
6. 快照、累计和全历史指标使用 `time_policy=FORBIDDEN`；已确认的默认时间偏好不能覆盖这种期间无关口径，趋势查询仍保持 `REQUIRED`。
7. 语义字段解析采用“正式名称/编码 > 注册同义词 > 包含匹配”的确定性优先级，并规范化“业务城市/业务市、业务省份/业务省”等等价标签；仍禁止根据物理字段名猜 Join。
8. 多个业务形状解析器产生的地区约束会按同一语义字段集合替换和去重，不再同时发送“业务城市=上海市”和“地区=上海市”。

## 10. 最终真实验收结果

语义模型均为 `81`，通过 `POST /agent_chat/stream` 真实调用：

| 验收问题 | 最终状态 | 结果 |
| --- | --- | --- |
| 查询空心纤维血液透析器产品合作的经销商名单 | `COMPLETED / DETAIL_QUERY` | 22 条唯一经销商，无指标、无默认时间 |
| 上一轮后追问“只展示前五名” | `COMPLETED / DETAIL_QUERY` | 正确保留原始 22 条结果集语义并返回前 5 条 |
| 查询上海市空心纤维血液透析器产品销售额 | `COMPLETED / METRIC_QUERY` | 单一标量 `1,221,915.7933`，未错误按产品分组 |
| 分析空心纤维血液透析器产品按月销售趋势 | `COMPLETED / TREND_ANALYSIS` | 返回月度趋势及业务解释，保留数据水位提示 |
| 查询上海市医用外科口罩经销商，排除上海洁安厂家，按整体业务规模排序 | `COMPLETED / METRIC_QUERY` | 连续 3 次稳定返回 7 行，SQL 均保留负向厂家条件 |

最后一个复合查询的最终结构为：

- 指标：`dealer_cumulative_sales_amount`
- 查询对象/分组：经销商
- 正向筛选：`业务城市 EQ 上海市`、`商品名称 EQ 医用外科口罩`
- 负向筛选：`厂家名称 NE 上海洁安`
- 排序：指标降序
- 时间：未提取，快照口径不注入默认时间
- 三层校验：`PASS`

## 11. 平台请求体兼容与执行契约修复

本次 422 的直接原因是平台工具配置发送了 `time_out`，而数据智能体此前只声明 `timeout`，并在外部请求模型上使用 `extra="forbid"`，因此请求尚未进入 Agent 执行链路就被 FastAPI/Pydantic 拒绝。

增量处理如下：

1. 外部平台输入边界改为前向兼容模式：未声明的扩展字段会被忽略；内部领域模型继续保持严格校验。
2. `tools[*].time_out` 在校验前规范化为运行时字段 `timeout`，数值会实际生效，而不是仅仅吞掉参数。
3. 同时出现 `timeout` 和 `time_out` 时，以标准字段 `timeout` 为准。
4. 已声明字段仍执行原有类型、范围和安全校验，例如非法 URL、非法 `semantic_model_id` 不会被放行。
5. 对“已合作医院数”等关系计数查询增加封闭的执行契约变换标记：允许把已绑定的关系计数指标转换为唯一名称集合后计数，但仍强制校验产品、地区、排除条件和排序，禁止借此绕过 Intent-ASL Contract。

验证结果：

- 请求模型/API 专项：`73 passed`
- 请求兼容、Intent-ASL Contract、意图、扩展调度等定向回归：`304 passed`
- 全量回归：`1149 passed, 26 failed`；失败数与修改前基线一致，新增 5 个用例全部通过，未增加失败项。
- 使用带 `time_out=15`、工具扩展字段和外层扩展字段的平台完整请求体真实调用 `POST /agent_chat/stream`：HTTP 200，进入 SSE 执行链路。
- “统计上海市紫杉醇释放冠脉球囊导管已合作医院数”完成为 `COMPLETED / METRIC_QUERY`。
- “查询最近一年销售过赛森尤斯产品的经销商名单”完成为 `COMPLETED / DETAIL_QUERY`；当前数据返回 0 行属于有效空结果，不再被误报为智能体配置错误。
