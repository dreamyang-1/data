# RB50-37 Real Targeted Result

**RB50_37_REAL_TARGETED_FAIL**

离线 Gate 和相关回归通过后，本轮只对 RB50-37 发起一次 CurrentTurn 和一次 SemanticEdits 调用。两次均为 HTTP 200，retry=0，没有前置轮调用、第三模型阶段、SQL 或生产写入。

## 第一失败阶段

`FIRST_FAILURE_STAGE = SEMANTIC_EDITS_DYNAMIC_SCHEMA_VALIDATION`

CurrentTurn 的动态 Schema 校验通过。它保留了完整具体商品 surface，并声明 `SUBJECT_ENTITY`、含税销售总额指标、`NEW_TASK` 和 `SCALAR_AGGREGATE`；没有把商品 surface 授权为 `FILTER_VALUE`。

SemanticEdits raw response 随后尝试生成 Source Value request 和使用该 request 的 filter。此次 exact issued dynamic Schema 明确要求：

- `source_value_requests.maxItems = 0`
- request 内 `field_binding_handles.maxItems = 0`

raw response 同时违反这两个 `maxItems` 约束。客户端在 Draft 消费、Source Value lookup 和 Task 发布之前返回稳定错误 `V2_MODEL_DYNAMIC_SCHEMA_VIOLATION`。

| 结果 | 状态 |
|---|---|
| CurrentTurn dynamic schema | PASS |
| SemanticEdits HTTP | 200 |
| SemanticEdits exact dynamic schema | FAIL |
| Draft consumed | NO |
| Source lookup reached | NO |
| Task published | NO |
| Silent broadening published | NO |
| Final classification | `SAFE_REJECT_MODEL_DYNAMIC_SCHEMA_VIOLATION` |

这不是 Source Value 读取、字段映射或 normalization 缺陷。真实 proof 已存在，且同一 proof 在离线回放中闭环成功；新 Live 在到达 proof 消费前已失败。本轮属于 Evidence Acquisition Round，指令禁止修改 Dynamic Schema、Prompt、Role、Provider 或 Entity preservation 合同，因此生产代码变更保持为 0。

## 调用与证据

- 模型：`qwen3.7-max`，Thinking=false，temperature=0，retry=0
- CurrentTurn：1 次，HTTP 200，3,647 tokens
- SemanticEdits：1 次，HTTP 200，20,557 tokens
- 合计：2 次，24,204 tokens，14.782 秒
- SQL：0；生产写入：0
- PRIVATE targeted result SHA-256：`ac6e5be07f59dc6fe82404d15525a7930cd4b71817748d0af0200ab2a46890fe`
- PRIVATE targeted analysis SHA-256：`34fdab67895594b144ee04b875157b1a0a55ede785d0363372eb88c177ea5d23`

此次结果不能记为业务正确 PASS，也不能重算 Core50。它只证明当前客户端的 exact dynamic-schema fail-closed 边界生效，并把下一根因定位在 CurrentTurn 实体实例表达与 SemanticEdits 可生成结构之间的对齐；本轮不启动该修复。
