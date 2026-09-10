# CURRENT_FILTER_OPERATION_PATH

本轮基线 `116ffe06d12812f849ba60eaa644df17cbc89f8a`，PR #62。以下是实际 `RawTurnPlanner` 调用顺序，不把 Grounding 画成模型 Draft 之前的步骤。

```text
Current Question / 当前可信 Scope / 已恢复 Task、Pending
  → 第一阶段 LLM：CurrentTurn Parse + Context Proposal
  → Context hard validation：Relation / TargetTask
  → CurrentTurnParser / Mention span、Role 候选校验
  → 当前 Catalog Pin 中的候选 handles + 当前 Filter target handles
  → 第二阶段 LLM：SemanticTaskDraft / FilterEditDraft / SourceValueRequestDraft
  → Pydantic Parser（FilterEditDraft.value 是 JsonValue）
  → source_filter_patch / resolve_requests
  → 消费引用检查 → 当前字段/目标校验 → Source Value exact/probe/choice/复验
  → choice_handles（只提供已证明的源值与对应字段）
  → _patch / initial_assignments / align_filter_deletions
  → 当前 mention、operation marker、role 和 handle 校验
  → hydrate_choice / Canonical Binding / structured_edits.filter_edits
  → TaskPatch → native Reducer → validate_source_value_fields
  → TaskSemanticState → Query Shape / SemanticQueryIR → LogicalPlan
  → Catalog finish / sealed state
```

源码锚点：`recognition.py::_run/_patch/_hydrate/value_schema`、`recognition_initialization.py::initial_assignments`、`source_value_recognition.py::resolve_requests/source_filter_patch`、`structured_edits.py::filter_edits`。新的 `filter_generation_schema.py` 仅被 `semantic_task_schema` 调用。

## 真实失败产物与第一拒绝边界

| Case | 上游已观察事实 | 错误 Filter / Request | 原生拒绝 | Oracle 后 |
|---|---|---|---|---|
| Round5.3 live J，江苏医院新任务 | NEW_TASK；目标为空；当前江苏 mention；源请求确实被消费；真实省份候选已选择 | ADD / target=null，value 仅为 value_request_id，缺 Predicate 的 field/operator/source/scope | `_patch` 的 V2_SLOT_OPERATION_CONFLICT；不满足空任务初始化表达条件 | 只替换 Filter value 为完整当前任务 Predicate 后，Canonical Binding、TaskPatch 和 Reducer 可观察；随后 CATALOG_RELATIONSHIP_REQUIRED |
| 原 B，带时间销售额初始查询 | NEW_TASK；时间、地区、指标 mention；当前源值候选已选择 | ADD / target=null，Predicate.scope=CURRENT_DATASET | V2_SLOT_OPERATION_CONFLICT | 只改 Filter value.scope 后进入 CATALOG_METRIC_TIME_ANCHOR_MISSING |
| 本轮 healthy A，地区替换 | REPLACE 和目标任务正确；当前过滤条件绑定 province_name | Filter REPLACE 本身合法；源请求却选择 city_name 字段，而非当前省份过滤目标 | `source_filter_patch → validate_source_value_fields` 抛 SOURCE_VALUE_FILTER_FIELD_MISMATCH | 只改 SourceValueRequest 的 field/target 选择后得到完整计划；这是源选择 Oracle，不能冒充 Filter-only Oracle |

J 的 Raw Parse 还声明了 RELATION_LIST，B 还带有目录不支持的销售额时间。这里定位的是本轮 Filter 路径的第一拒绝及其 Producer 产物，并未宣布它们在完整语义链上只有一个错误。下游分歧不重复统计为已关闭的 Filter Root。

## A–J 根因分类

- **C / F / G：PROVEN**。原导出 Schema 用无限定 JsonValue 表示 Filter operand，接受 J 的裸源值和 B 的 CURRENT_DATASET；消费端已有 ADD/new → 当前显式 Predicate、targeted → typed value/subtree 的不同约束。模型错误输出与生成约束缺失分别记录，不能证明约束补齐一定让模型成功。
- **B / D：NOT_PROVEN**。现有内部合同已经能表达正确结果，未发现 Parser 把合法产物转坏，也未发现必须新增 Schema primitive。保持全部原有运行时类型和 Validator。
- **E / H：本轮 A 的 Producer 字段选择矛盾已证实**。XOR 分支已满足，问题是选择 city 源字段与既有 province Filter target 不相容。运行时拒绝正确，不能自动替换字段或掩盖歧义。
- **I：没有用 Filter 层修补 Mention/Role/Grounding**。J/B 原有 mention 与源候选在 Filter-only Oracle 中保持不变；A 的源选择 Oracle明确改变 Grounding 字段，单独记账。

没有任何原 J/B Filter-only Oracle 得到完整 LogicalPlan，所以 **FILTER_OPERATION_CAUSAL_ROOT_CONFIRMED（完整通过含义）= NO**。已证实的是 Filter 拒绝的局部因果性，NEXT_DIVERGENCE 明确保留。A 的完整源选择 Oracle PASS 也不是 Live、模型或 Gold PASS。

## 本轮最小修改

复用既有 `value_schema()` 的 Predicate、AliasedPredicate、typed value 和 opaque handle 定义，导出 ADD/new、ADD/REPLACE target、REMOVE target、CLEAR target 的结构分支。新增条件 source=USER_EXPLICIT、scope=CURRENT_TASK 来自原生 `structured_edits.filter_edits`，不是新业务默认值。

没有创建新的 Filter Schema primitive，没有把一个裸值自动包装成 EQ Predicate，没有更换字段、目标、source value、候选集合、操作或作用域。JSON Schema 仍不能替代全部跨字段、跨 Artifact、成员资格、Boolean placement 和 Catalog 约束；这些继续由原有确定性校验负责。Prompt 文本、TaskPatch、Reducer、Source Value 检索主结构不变。

专项正反例覆盖 source ref、typed list、scalar、alias Predicate、ADD/REPLACE/REMOVE/CLEAR、非法 target、错误 scope、裸值和伪造 authority。真实 Runtime 对原非法产物仍拒绝；生成 Schema 无法替代模型语义质量验收。
