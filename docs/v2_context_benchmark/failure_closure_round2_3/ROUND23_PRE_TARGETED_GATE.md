# Round 2.3 Pre-targeted Gate

**ROUND23_PRE_TARGETED_GATE = FAIL**

| 项目 | 结果 |
|---|---|
| ROOT_CAUSE | `CURRENT_TURN_ENTITY_INSTANCE_LOST` |
| FIRST_DIVERGENCE | CurrentTurn 把具体产品 surface 只声明为 `SUBJECT_ENTITY/subject SET`，没有任何 Entity Value 语义归宿 |
| CHANGED_FILES | `source_value_recognition.py`、`recognition.py`、1个新增专项测试；报告文件另列 manifest |
| ENTITY_REPRESENTATION | 复用 `filter_expression + FILTER_FIELD + EntityValueRef + SourceValueBindingEvidence` |
| UNIT_TEST_RESULT | 12 passed |
| REGRESSION_RESULT | 219 passed，0 failed，0 collection errors |
| A_E_RESULT | `PASS_OFFLINE_REGRESSION`；Context Critical Slice、completed question、Canonical bridge 全部通过；未调用8088 |
| OFFLINE_RB50_37_RESULT | `SAFE_BLOCKED_MISSING_FROZEN_INSTANCE_PROOF` |
| GENERIC_SUBJECT_NEGATIVE_CONTROL | PASS；Generic“医院”保持 Subject/Dimension，Source Value lookup 为0 |

## Gate 未通过的唯一原因

离线 replay 精确复用了 Round 2.2 的两份 RB50-37 模型响应，并在断网环境使用同一冻结 Catalog 和 Source Value artifact。新逻辑识别到该 surface 不能静默退化成泛化产品类型，随后尝试从 Catalog 声明的 `product_name` 字段获取 exact proof。旧冻结 artifact 没有这条 observation，按合同返回：

`FROZEN_SOURCE_VALUE_OBSERVATION_REQUIRED`

因此：

- Task 没有发布；
- “全部产品”的静默扩大已被阻止；
- 具体商品约束尚未在该真实 replay 中得到证明；
- completed question 和 Canonical 正向验收不具备真实目标 Case 的前提。

按照 Round 2.3 指令，Gate 失败后不得调用模型。本轮新增 CurrentTurn 调用=0、SemanticEdits 调用=0，RB50-10 也未追加运行。
