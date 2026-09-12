# Round 2.2 Dynamic Schema Enforcement

**ROUND22_SCHEMA_ENFORCEMENT_CHECKPOINT = PASS**

| Checkpoint item | Result |
|---|---|
| `CHANGED_FILES` | `app/semantic_v2/recognition_client.py`、`pyproject.toml`、1个新增专项测试及因入口拒绝点前移而迁移的既有测试；Benchmark、scorer和业务期待未改 |
| `UNIT_TEST_RESULT` | Round 2.2专项 **12 passed** |
| `REGRESSION_RESULT` | 最终受影响离线集合 **346 passed / 0 failed / 0 collection errors** |
| `A_E_RESULT` | 原接口完成问题与桥接专项 **11 passed**，包含在346项内 |
| `ERROR_BEHAVIOR_BEFORE` | RB50-10/37为 `V2_SOURCE_VALUE_CURRENT_MENTION_REQUIRED`，RB50-25为 `V2_SOURCE_VALUE_REQUEST_NOT_APPLIED`；违规Draft已进入Runtime |
| `ERROR_BEHAVIOR_AFTER` | 三条既有违规raw response均在模型客户端入口返回 `V2_MODEL_DYNAMIC_SCHEMA_VIOLATION`；Runtime未消费，离线复验模型调用0 |

`RecognitionModelClient.complete` 现在按本次调用实际下发的同一个 JSON Schema，在静态 Pydantic 输出模型之前校验 raw JSON。该修改只收紧模型输出边界，不修改 Prompt、CurrentTurn/SemanticTaskDraft、Role、Source Value语义、Runtime guard、Scope或查询形状。

## 调用顺序

修改前：

```text
raw content
→ output_model.model_validate_json(content)
→ Runtime
```

修改后：

```text
raw content
→ JSON parse
→ exact issued Draft 2020-12 schema validation
→ existing output_model.model_validate_json(content)
→ Runtime
```

客户端没有重新生成近似 Schema。用于 system message、可选 Provider `json_schema` 请求以及本地校验的都是 `complete(..., schema=...)` 确定后的同一个对象。

## 错误合同

| 条件 | 结果 |
|---|---|
| raw JSON语法错误 | 保持 `V2_MODEL_OUTPUT_INVALID` |
| raw JSON违反本次动态 Schema | `V2_MODEL_DYNAMIC_SCHEMA_VIOLATION` |
| 下发 Schema自身非法或引用无法解析 | `V2_MODEL_DYNAMIC_SCHEMA_INVALID` |
| 动态 Schema通过、静态 Pydantic失败 | 保持 `V2_MODEL_OUTPUT_INVALID` |

所有失败均 fail closed。客户端不删除字段、不改写 Role、不重试模型、不增加模型阶段，也不把非法 Draft降级为合法对象。

Round 2.1 的 RB50-10、RB50-37、RB50-25 raw SemanticEdits 响应继续通过原静态 `SemanticTaskDraft`，但在新边界均得到 `V2_MODEL_DYNAMIC_SCHEMA_VIOLATION`。该离线复验为3/3，模型调用0。

## 修改文件

- `app/semantic_v2/recognition_client.py`
- `pyproject.toml`：将实际生产导入的 `jsonschema` 声明为运行依赖
- `tests/test_v2_dynamic_schema_enforcement_round22.py`
- 既有 Recognition、Source Value、Context测试：只把明确违反动态 Schema的故障注入改为入口拒绝期待，并把合法 Source Value fixture切换为动态 Schema已经声明的 selector；Benchmark数据和评分期待未改

## 验证

专项12项覆盖：动态 Schema和Pydantic同时通过、空 Schema 保持原样、空 `maxItems=0`、合法 enum、两个 `maxItems=0` 违规、非法 enum、`anyOf`违规、坏 JSON、动态通过后Pydantic失败、非法 Schema及无法解析的 `$ref`。每个故障用例确认没有模型重试。

最终受影响集合覆盖 Recognition、Round1 Source Value、Targeted Source Value、Filter generation、Context proposal、A–E完成问题/API桥接。完整结果记录在 [round22_summary.md](round22_summary.md)。测试使用 Mock/冻结数据，模型调用和SQL调用均为0。

这项修改证明的是工程安全边界。它不证明模型更可能产生正确 Draft，也不改变历史 V2 5/37、First Task Publication 2/30 或任何 Core50分数。
