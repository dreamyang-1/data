# Current Source Value / Filter Target Alignment Path

本轮从 PR #63 / `67f2c8022d1014ee4f6dc3af17f677d12baa93ca` 的真实 HC54-A capture 开始。所有因果实验先于生产修改；原 capture、Catalog 与源值观察未覆盖。完整输入及逐阶段对象仅保存在 PRIVATE，公开文件保留 hash、合同和结论。

| 阶段 | 原始实际行为 | 本轮处理 |
|---|---|---|
| Current Question | “换成北京市” | 原句保持 |
| Context Proposal / Relation / Target | REPLACE，选择实际江苏订单任务、版本1 | 保持；不增加规则仲裁 |
| Mention / Role | 北京市，FILTER_VALUE，filter_expression REPLACE marker | 保持 |
| Current Filter Target | `province.province_name = 江苏省`，已有精确 Filter handle | 继续作为当前任务授权状态中的事实 |
| Field Candidates | FILTER_VALUE 可提出多个可查询 Attribute；包含城市名称、省份名称 | 不扩大枚举、Top-K 或 Scope |
| SourceValueRequest | 独立选 `city.city_name`，没有填写已有 target_filter_handle | 生成 view 对当前值 REPLACE/REMOVE 导出 target-selector 分支 |
| Source Retrieval / Selection | 精确查到 city 下的“北京市”；候选带 city 字段身份 | 消费前核对请求字段与使用该请求的 Filter Target canonical ID；错误字段直接拒绝，不读取该当前字段值 |
| Grounding Validation | city 候选自身合法，但不能代表 province 值 | 不引入跨字段等价推断 |
| Filter Operation / Canonical Binding | 省份 Predicate 的值编辑尝试使用城市实体值 | 原最终 field identity 校验仍保留 |
| TaskPatch / State / IR / Plan | 原始路径拒绝，未发布错误状态或计划 | 合法 target-selector 后通过真实编译、Reducer、IR、LogicalPlan；不修改这些组件 |

主分类 **C：Field selection 允许不兼容字段**；共同生成合同缺口为 **F：Schema 能表达已有 selector，但生成 view 没有导出目标约束**。FilterEdit 携带了正确目标，Adapter 未丢目标，Source Value Consumer 已存在，Grounding 也保留了字段身份；A/D/G/H 和 Consumer Missing 均不符合这条原始证据。

`source_value_target.validate_requested_fields` 只比较已验证目录中的 canonical field identity。任何消费同一 source request 的目标字段都必须一致；ADD 到已有 Predicate 同样受约束。新条件、无可用目标及 ADD 新 Predicate 保留既有选择方式。相同字段的旧 fields-selector 输入仍可被 Runtime 消费。模型仍负责选择已有目标；没有替模型选 Top1，没有城市/省份字符串特判，没有推断 Name/Code 等价。

`source_target_schema` 只改变发送给第二阶段模型的动态 JSON Schema。Prompt 文本、模型、Context、Runtime Schema primitive、Reducer 均未改。Provider 的 json_object 模式不保证遵守此生成 view，因此消费前校验与最终校验都不可省略。Chain C 的非法字段请求被前置校验实际挡住，未伪装成模型已普遍遵守新合同。

## 单变量实验的严格边界

1. **保持原 city Candidate 的负控制**：仅调整 request selector 指向当前省份目标，同时锁定 resolver 产生的原 city Candidate，仍然字段不匹配。没有把 city ID 改写为 province ID，也没有宣布这一严格 Oracle PASS。`SOURCE_VALUE_FILTER_ALIGNMENT_CAUSAL_ROOT_CONFIRMED` 的严格 candidate-fixed 条件未满足。
2. **仅修正 SourceValueRequest selector 的对照**：其余模型输出、FilterEdit、Context、TaskState、Scope、Pin 和冻结源值观察集合保持；由省份目标重新取得正式 province 候选，真实 State/IR/Plan 通过。字段变化必然导致 candidate identity 变化；这是额外的 source-selector 对照，不能冒充上面的同 Candidate Oracle，更不算 Live/Gold PASS。
3. **修改后的原输出消费回归**：原 city request 在当前值查询前拒绝；修正 selector 的回归通过。因为生成 Schema 已变化，不称为输入完全一致的严格 Recorded Replay。
4. **实际 Live A**：模型自己选中已有 Filter Target，province 下的北京市候选绑定成功；后续 CLEAR、Time REPLACE 通过独立计划检查。真实 Live 成功与上述 Oracle 分开计数。

结论为 **确定性字段合同已加强，Live 泛化尚未关闭**。本轮没有将安全拒绝计为业务成功。
