# 《New Topic / Follow-up Context Leakage 修复报告》

## 1. 修复范围

本次暂停继续扩展 Long-range Conversation Recall，只增量修复以下链路：

`Turn Relation Classification → Context Inheritance Gate → Explicit Slot Protection → Stale Context Cleanup → Context Consistency Validation → Query-to-SQL Entity Alignment`

未重写 Memory、LangGraph、Analysis Thread、Skill 或 Tool 架构，未引入新的向量库、图数据库或 Agent。

## 2. 根因定位

失败案例：

- Turn 1：`查询空心纤维血液透析器产品合作的经销商名单。`
- Turn 2：`按月分析外周插管中心静脉导管的销售趋势。`

修复前的真实链路是：

1. 原始 Turn 2 单独经过规则解析时，能够识别 `TREND_ANALYSIS`，但产品名不以“产品”结尾时没有形成当前显式产品槽位。
2. `QuestionRewriter._apply_context()` 在轮次关系准入之前运行，把“按月分析……”误当成粒度追问，将上一轮的 `经销商 / DETAIL_QUERY / 空心纤维血液透析器产品` 写入改写问题。
3. `rewrite.context_applied=true` 触发 `merge_clarification(previous, current)`，上一轮请求成为 merge base。
4. 因当前产品未形成受保护的显式槽位，旧产品过滤器、经销商维度和查询对象没有被清除。
5. Semantic Resolver、ASL、SQL 只是在消费已经被污染的 Canonical Request，并不是污染源。

根因分类结论：

| 分类 | 结论 |
|---|---|
| A. Turn Classification | 结果受污染，但原始句单独分类正确；根因是分类前已引入历史文本 |
| B. Query Rewrite | **主因**：发生在准入判断之前，并提前补入旧业务槽位 |
| C. Historical Recall | 本案例未触发，不是根因 |
| D. Context Merge | **主因**：以旧请求为 base，缺少 relation/provenance 约束 |
| E. Explicit Entity 被覆盖 | **主因之一**：无“产品”后缀的产品名没有形成可保护槽位 |
| F. State 未清理 | **共同原因**：New Topic 仅覆盖部分值，没有清除旧维度、对象和过滤器 |
| G. Semantic Resolver | 下游受害者，不是污染源 |
| H. Prompt 补全 | 不独立构成根因；危险点已由代码级 Gate 截断 |

## 3. 修改后的执行顺序

```text
Raw Query
→ CurrentTurnFacts（只读取当前原始句）
→ Explicit Slot Extraction
→ Self-contained / Reference Signal Check
→ Turn Relation Classification
→ Context Inheritance Gate
→ 必要时读取 Current/Historical Thread
→ Slot-aware Merge
→ CURRENT_EXPLICIT 再保护
→ Stale Slot Cleanup
→ Context Consistency Validation
→ Canonical Query
→ Semantic Resolution / Planner / Skill
→ SQL Translation
→ Query-to-SQL Entity Alignment
→ SQL Execute
```

只有以下 relation 允许继承业务上下文：

- `CURRENT_TOPIC_FOLLOWUP`
- `CURRENT_TOPIC_MODIFICATION`
- `CURRENT_TOPIC_DRILLDOWN`
- `HISTORICAL_TOPIC_RETURN`
- `CLARIFICATION_RESPONSE`
- `CORRECTION`

`STANDALONE_NEW_TOPIC` 使用 `context_mode=NONE`，在同一个 `conversation_id` 内创建新的 `analysis_thread_id`，并清除上一任务的业务槽位。

## 4. 主要实现

### 4.1 Turn Admission Gate

新增 `TurnAdmissionGate`，先对当前 raw query 提取事实，再与结构化 active task 比较。当前原始句不会读取上一轮自然语言、Conversation Summary 或 Long-term Memory。

结构化结果包含：

- `relation / confidence / context_mode`
- `is_self_contained`
- `core_subject_changed`
- `reference_signals / followup_signals / topic_shift_signals`
- `inherit_business_context`
- `protected_slots / cleared_slots / inheritance_slots`
- `previous_thread_id / selected_thread_id`
- `context_before / context_delta / context_after / context_conflicts`

### 4.2 Explicit Slot Protection

实现固定优先级：

`CURRENT_EXPLICIT > CURRENT_CLARIFICATION > CURRENT_INFERRED > RESTORED_EPISODE > ACTIVE_THREAD_STATE > RECENT_CONTEXT > CONVERSATION_SUMMARY > LONG_TERM_MEMORY`

当前显式产品、地区、分析类型、时间粒度、过滤器等在 merge 后重新应用。历史值不能覆盖 `CURRENT_EXPLICIT`。

补充了两类产品识别：

- 完整任务：`按月分析外周插管中心静脉导管的销售趋势`
- 省略追问：`外周插管中心静脉导管呢？`

### 4.3 Stale Context Cleanup

New Topic 会清理旧任务的：

- `analysis_type`
- `entity / query_object`
- `fields / dimensions`
- `filters`
- `time_range`
- `comparison / top_n`
- `previous ASL / result reference`

旧值可以保留在只读审计字段 `context_before` 中，但不会进入 `context_after`、Canonical Query、ASL 或 SQL。

### 4.4 两层 Fail-closed 校验

1. Context Consistency Validator：当前显式槽位与合并后上下文冲突时，先从当前原始句重建一次；仍冲突则以 `STALE_CONTEXT_CONFLICT` 停止。
2. Query-to-SQL Entity Alignment：SQL 执行前比较当前显式实体与 SQL；当前实体缺失或旧实体仍存在时，分别以 `SQL_QUERY_ENTITY_ALIGNMENT_FAILED` / `STALE_CONTEXT_CONFLICT` 停止，错误 SQL 不会执行。

## 5. 修改文件

| 文件 | 修改内容 |
|---|---|
| `app/domain/models.py` | TurnRelation、ContextMode、SlotSource、SlotProvenance、CurrentTurnFacts、TurnAdmissionDecision、SQL 对齐结果模型 |
| `app/services/turn_admission.py` | 准入判断、显式槽位提取与保护、上下文快照/冲突校验、线程决策 |
| `app/intent/classifier.py` | 无“产品”后缀的明确产品分析句绑定 |
| `app/services/orchestrator.py` | 将 Admission Gate 前移到 Rewrite/Recall/Merge 之前；New Topic 新建线程；记录上下文审计事件 |
| `app/adapters/http.py` | SQL 执行前 Query-to-SQL Entity Alignment |
| `app/stores/events.py` | 新增 `TURN_ADMISSION`、`CONTEXT_MERGE` 事件 |
| `app/observability/tracing.py` | 新增准入、继承、上下文冲突和 SQL 对齐 Trace Span |
| `tests/test_turn_admission.py` | 7 个指定案例、SQL 拦截、真实双轮编排回归 |
| `scripts/run_context_leakage_regression.py` | 8088 真实服务双轮回归与 Redis 审计事件输出 |

## 6. 回归测试

指定的 7 个场景均已覆盖：

| Test | 预期 relation | 结果 |
|---|---|---|
| A 经销商查询 → 完整 B 月度趋势 | `STANDALONE_NEW_TOPIC` | PASS |
| A 趋势 → “B 呢？” | `CURRENT_TOPIC_MODIFICATION` | PASS |
| A 趋势 → 完整 B 月度趋势 | `STANDALONE_NEW_TOPIC` | PASS |
| A 经销商 → “销售趋势呢？” | `CURRENT_TOPIC_FOLLOWUP` | PASS |
| A 经销商 → 完整 B 趋势 | `STANDALONE_NEW_TOPIC` | PASS |
| 上海 A 趋势 → “北京呢？” | `CURRENT_TOPIC_MODIFICATION` | PASS |
| 上海 A 趋势 → 广东 B 医院销量排名 | `STANDALONE_NEW_TOPIC` | PASS |

测试命令及结果：

```text
pytest -q tests/test_turn_admission.py
9 passed

pytest -q tests/test_turn_admission.py \
  tests/test_working_memory.py::test_orchestrator_can_return_to_an_older_task_branch \
  tests/test_orchestrator.py::test_granularity_only_follow_up_inherits_metric_and_becomes_trend \
  tests/test_orchestrator.py::test_history_recovers_clarification_after_short_memory_is_missing
12 passed
```

## 7. 真实双轮验收

服务：`http://127.0.0.1:8088`  
模型：`semantic_model_id=81`  
会话：`context-leakage-regression-20260901034703408262`

### 7.1 Turn 2 完整 Debug 摘要

```yaml
raw_query: 按月分析外周插管中心静脉导管的销售趋势。
explicit_slots:
  product: {value: 外周插管中心静脉导管, source: CURRENT_EXPLICIT}
  analysis_type: {value: TREND_ANALYSIS, source: CURRENT_EXPLICIT}
  time_grain: {value: month, source: CURRENT_EXPLICIT}
  filters:
    - {field: 商品名称, operator: EQ, value: 外周插管中心静脉导管}
inferred_slots:
  metrics: [销售额]
is_self_contained: true
reference_signals: []
previous_thread: thread-3dd5a5b439404db3a3e973bd3158691b
selected_thread: thread-8c22793b27004c8f99ad6b503db6daeb
turn_relation: STANDALONE_NEW_TOPIC
core_subject_changed: true
inheritance_allowed: false
protected_slots: [analysis_type, filters, product, time_grain]
cleared_slots: [analysis_type, entity, fields, dimensions, filters, time_range]
context_conflicts: []
```

`context_before`（仅审计）包含上一任务的 `DETAIL_QUERY / 经销商 / 空心纤维血液透析器产品`。

`context_after`（实际执行）为：

```json
{
  "intent": "TREND_ANALYSIS",
  "entity": "产品",
  "metrics": ["销售额"],
  "dimensions": [],
  "fields": [],
  "filters": [
    {"field": "商品名称", "operator": "EQ", "value": "外周插管中心静脉导管"}
  ],
  "comparison": null,
  "top_n": null
}
```

Canonical Query：

```text
分析趋势；指标：销售额；对象：产品；时间范围：2025-09-01 至 2026-09-01（含首尾）；时间粒度：按月；过滤条件：[{"field":"商品名称","operator":"EQ","value":"外周插管中心静脉导管"}]
```

Semantic Resolution：

```yaml
metric_id: 81:annual_total_sales
canonical_name: 含税销售总额
aggregation: SUM
entity: 产品
time_grain: month
business_rule: sales_order.quantity > 0
selected_skill: trend_analysis
```

实际 SQL：

```sql
SELECT DATE_FORMAT(sales_order.created_date, '%Y-%m') AS `交易日期`,
       SUM(sales_order.amount_with_tax) AS `含税销售总额`
FROM sales_order
LEFT JOIN product ON sales_order.product_code = product.product_code
WHERE sales_order.quantity > 0
  AND product.product_name = '外周插管中心静脉导管'
  AND sales_order.created_date >= '2025-09-01'
  AND sales_order.created_date < '2026-09-02'
GROUP BY DATE_FORMAT(sales_order.created_date, '%Y-%m')
ORDER BY 交易日期 ASC;
```

SQL 校验：

```yaml
SQL_SEMANTIC: PASS
QUERY_TO_SQL_ENTITY_ALIGNMENT: PASS
current_entities: [外周插管中心静脉导管]
stale_entities: []
row_count: 3
quality_status: PASS
```

最终回答：

> 已验证结论：2025年10月至12月，外周插管中心静脉导管的含税销售总额整体呈下降趋势，从3,623,360.46元降至1,042,521.39元，累计降幅为71.23%。销售波动主要由2025年10月至11月的急剧下跌驱动，该期间销售额减少3,109,957.28元；随后11月至12月出现回升，增加529,118.21元，但仅收复了此前跌幅的17.01%，尚不能判断为趋势反转。

验收结论：

- `context_after` 无“空心纤维血液透析器”——PASS
- Canonical Query 无旧产品、经销商维度和 `DETAIL_QUERY`——PASS
- ASL / SQL 无旧产品——PASS
- Query-to-SQL 对齐报告 `stale_entities=[]`——PASS
- 最终回答无旧产品和经销商查询语义——PASS

## 8. 剩余风险

1. 当前原始句产品识别采用“确定性规则 + 现有实体抽取服务”，新型省略表达或新的器械后缀仍需继续补充词典/实体模型；即使漏提取，新增的一致性与 SQL 对齐门禁仍会阻止已识别显式实体被错误替换。
2. SQL 对齐当前要求当前显式名称以字面值出现在 SQL 中。未来若 SQL Translator 改成只使用内部 ID 参数，需要同时返回 `current entity → bound ID → SQL parameter` 的可审计绑定证据，避免误报。
3. 真实验收中 Turn 1 的上游语义查询曾返回 `ASL_METRIC_SELECTION_INVALID`，但 task frame 已在外部调用前正确保存，因此不影响本次 Turn 2 的新话题隔离验收。该上游明细查询口径冲突属于另一问题，不在本次上下文串线修复范围内。
4. 当前业务数据水位截止 2025-12-30，因此默认近一年范围的水位后区间只做边界提示，不推断不存在销售。
