# Follow-up Context Inheritance 与时间结果复用修复报告

## 1. 修复范围

本次只修复多轮分析中的 under-inheritance，不扩展 Long-range Conversation Recall，也不重构 Memory、LangGraph 或 Agent 数量。

专项范围：

- Follow-up Dependency Detection
- Slot-aware Conditional Inheritance
- Conversation-aware Time Resolution
- Previous Result Sufficiency Check
- Derived Result Calculation
- Clarification Gate 前置复用

## 2. 真实根因

失败问题：`11月较10月下降多少？`

修复前真实中间状态：

```text
raw intent: METRIC_QUERY
raw time_range: 2026-11-01 ~ 2026-11-30
raw missing_slots: [metric]
reference_signals: []
followup_signals: []
turn_relation: AMBIGUOUS_RELATION
inherit_business_context: false
```

根因由三个缺口共同导致：

1. Turn Admission 只识别“呢、为什么、继续、纯时间”等表面追问，没有识别“两个裸月份 + 比较动作”的结果依赖。
2. 独立时间解析先于会话时间锚定，把裸 `11月` 直接绑定到系统当前年份 2026。
3. Planner 没有在 Clarification 前检查上一轮不可变结果是否已经足以回答，因此继续追问指标和比较方式。

Historical Recall 未误触发；问题发生在当前 Active Thread/Episode 的条件继承和时间引用解析阶段。

## 3. 修改后的链路

```text
Raw Query
→ Current Explicit / Reference Extraction
→ Follow-up Dependency Detection
→ Turn Relation
→ Slot-aware Inheritance
→ Conversation Temporal Resolution
→ Comparison Intent Resolution
→ Previous Result Sufficiency Check
├─ sufficient: Derived Calculation → Answer（无 SQL）
└─ insufficient: Semantic Query / Drilldown SQL
```

新问题隔离逻辑保持不变：完整 B 产品新任务仍是 `STANDALONE_NEW_TOPIC`，不会继承 A 产品。

## 4. 关键实现

### 4.1 结果比较追问识别

新增确定性信号：

- `RESULT_PERIOD_COMPARISON`
- `RESULT_PERIOD_RECOVERY`
- `RESULT_DELTA_ELLIPSIS`

`11月较10月下降多少` 现在被判断为：

```text
turn_relation: CURRENT_TOPIC_FOLLOWUP
context_dependency: true
followup_type: RESULT_COMPARISON_FOLLOWUP
query_resolution_type: DERIVED_RESULT_QUERY
```

### 4.2 裸月份不再直接使用系统年份

当前句中的 `11月/10月` 首先保存为 `temporal_reference`，而不是 `2026-11`。

当 Turn 属于当前话题追问时，年份按以下顺序解析：

1. 当前显式完整年份
2. Active Episode 的实际可用期间
3. Active Thread 的时间范围
4. 系统当前日期（仅独立新问题）

解析后的槽位来源为 `CURRENT_REFERENCE_RESOLUTION`。

### 4.3 Temporal Anchor

上一轮查询执行成功后，从真实结果时间列构建：

```json
{
  "range_start": "2025-10-01",
  "range_end": "2025-12-31",
  "grain": "month",
  "available_periods": ["2025-10", "2025-11", "2025-12"]
}
```

请求时间范围可以宽于实际数据，但会话引用优先使用结果中真实存在的期间。

### 4.4 Previous Result Sufficiency

复用现有 MinIO Conversation Dataset，不新增结果记忆系统。

仅在以下条件全部满足时跳过 SQL：

- 最新结果属于当前 tenant/user/application/conversation
- 语义模型和业务域一致
- 时间列唯一可识别
- 指标列唯一可识别
- 左右期间各恰好一条可用值
- 数值有限且可计算

不满足时 fail closed，进入正常 SQL/Drilldown 链路。

### 4.5 Clarification Gate

结果复用检查位于 `missing_slots` 澄清之前。

因此上一 Thread 已明确产品、地区、指标且 Previous Result 已包含两个月份时，不再询问：

- “要查询哪个指标？”
- “希望同比、环比还是对象间比较？”

### 4.6 查询解析审计

新增 `QUERY_RESOLUTION` 事件及 Langfuse span，记录：

- raw_query
- turn_relation
- active_thread / active_episode
- context_dependency
- omitted_slots / inherited_slots
- temporal_reference / temporal_anchor
- resolved_periods
- comparison
- previous_result_available
- result_sufficiency
- execution_mode
- source_dataset_id

## 5. 修改文件

| 文件 | 修改内容 |
|---|---|
| `app/domain/models.py` | TemporalAnchor、ResolvedPeriodComparison、结果复用和审计字段 |
| `app/services/turn_admission.py` | 结果依赖检测、裸月份引用、遗漏槽位和继承来源 |
| `app/services/conversation_followup.py` | 会话时间解析、结果充分性校验和确定性比较计算 |
| `app/services/orchestrator.py` | 时间解析接入、澄清前结果复用、无 SQL 回答和审计事件 |
| `app/intent/classifier.py` | 防止产品名错误吸收尾随时间范围 |
| `app/stores/events.py` | `QUERY_RESOLUTION` 事件类型 |
| `app/observability/tracing.py` | Turn/Context/Result Resolution 的 Trace span |
| `tests/test_conversation_result_followup.py` | 六组专项回归 |
| `scripts/run_followup_result_reuse_regression.py` | 8088 真实双轮验收脚本 |

## 6. 六组回归结果

| 场景 | 预期 | 结果 |
|---|---|---|
| 2025-10~12 趋势 → 11月较10月下降多少 | 继承范围并复用结果 | PASS |
| 2025-10~12 趋势 → 12月恢复多少 | 2025-12 vs 2025-11 | PASS |
| 趋势 → 为什么11月下降 | 继承作用域，结果不足，重新查询 | PASS |
| 2025趋势 → 2024年11月呢 | 显式2024优先 | PASS |
| 销售额趋势 → 11月销售量呢 | 年份继承，指标覆盖 | PASS |
| A趋势 → B产品2026年11月趋势 | 新话题，不继承A | PASS |

专项与上轮防泄漏组合测试：`18 passed`。

扩展相关旧回归：`225 passed, 5 failed`。5 个失败与修改前已知基线一致，集中在旧默认时间和旧澄清契约，不是本次新增回归。

## 7. 真实 8088 双轮验收

Conversation：`followup-result-reuse-20260901041215960070`

Turn 1：

```text
分析上海市紫杉醇释放冠脉球囊导管整体销售趋势。
```

结果期间和值：

```text
2025-10: 2,504,250.3023
2025-11:   564,450.0290
2025-12: 1,054,699.9866
```

Turn 2：

```text
11月较10月下降多少？
```

完整关键 Debug：

```yaml
raw_query: 11月较10月下降多少？
turn_relation: CURRENT_TOPIC_FOLLOWUP
context_dependency: true
active_thread: thread-2dba483bb681449aa57dc77448ddd76c
active_episode: 8e05c5f8-cfe4-437d-b610-03f1a1212f4d
omitted_slots:
  - subject
  - metric
  - time_year
  - business_filters
inherited_slots:
  - analysis_type
  - entity
  - metrics
  - filters
  - time_range
temporal_reference:
  - 11月
  - 10月
temporal_anchor:
  available_periods: [2025-10, 2025-11, 2025-12]
resolved_periods: [2025-11, 2025-10]
comparison:
  comparison_type: PERIOD_TO_PERIOD
  left_period: 2025-11
  right_period: 2025-10
  operation: DECLINE
previous_result_available: true
result_sufficiency: true
execution_mode: REUSE_PREVIOUS_RESULT
sql_tool_calls: 0
```

最终回答：

> 2025年11月较2025年10月下降1,939,800.27元，变动幅度为77.46%。其中，2025年10月为2,504,250.30元，2025年11月为564,450.03元。

验收结论：

- 未追问：PASS
- 未使用 2026 年：PASS
- 产品、上海地区和含税销售总额均来自 Active Thread：PASS
- 2025-11 vs 2025-10：PASS
- Previous Result 充分性为 true：PASS
- 第二轮 SQL 调用数为 0：PASS
- 最终回答直接给出下降金额和比例：PASS

## 8. 剩余风险

1. 同一个月份在 Previous Result 中跨多个年份重复出现时，系统会拒绝猜年份并回退查询/澄清。
2. 同一月份存在多行分组明细时不会擅自求和；只有上一结果已经是唯一期间聚合值才允许复用。
3. “为什么下降”只继承作用域，Previous Result 不足以证明原因时仍会进入归因查询，这是预期行为。
4. 当前结果复用以月度期次比较为首个受控算子，季度、周、同比和累计恢复率可在后续独立扩展。
