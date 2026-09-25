# V2 Context → V1 Scope Contract Validation

**MODEL_WIDE_SCOPE_CONTRACT_COMPLETE**

本次修正只处理 `V2_CONTEXT_V1_EXECUTION` Bridge 的请求范围合同。未指定
`business_domain_ids` 时，调用方授权继续保持 `MODEL_WIDE`；Bridge 使用当前已审查的
模型目录范围 `[205]` 解析和固定 Catalog，但不会把请求改写成显式域授权。
`department` 是平台兼容元数据，不参与 Semantic Scope 判断。

| 合同 | 结果 |
|---|---|
| model 81，未指定业务域 | PASS；`MODEL_WIDE`，requested=`[]`，resolved=`[205]` |
| model 81，显式 `[205]` | PASS；`EXPLICIT_DOMAINS`，requested/resolved=`[205]` |
| model 81，显式非授权域 | FAIL CLOSED；`V2_CONTEXT_V1_SCOPE_PIN_MISMATCH` |
| `department=ORG_ADMIN`，未指定业务域 | PASS；仍为 `MODEL_WIDE`，department 不参与解析 |

## 实现边界

- Bridge 在入口分别保存 requested scope、resolved catalog scope 和 selection mode。
- `AuthorizedSemanticScope` 始终来自当前 `ChatRequest`；空列表不会被改写为 `[205]`。
- `ScopedPlanSession` 允许 `MODEL_WIDE` 请求在已审查的 `[205]` Catalog snapshot 上规划，
  并把该 resolved scope 写入 Snapshot；显式域仍必须与 resolved scope 完全一致。
- RawTurnPlanner 只增加可选的 resolved Catalog scope 透传；默认调用行为不变。
- 没有使用 `department`、角色、历史、Pending 或模型输出创建或扩大授权。
- 没有修改 V1 业务逻辑、V2 语义判断、Prompt、Oagnet、SQL Translator 或 Java 平台。

## 纯 V1 历史合同

`MODEL_WIDE` 合同由提交
`1c28cd64e0422c98ad4bb3b0222701f63845984f`（2026-09-08，
`phase0c: enforce trusted semantic scope and isolate state`）引入：空
`business_domain_ids` 生成 `AuthorizedSemanticScope(scope_mode="MODEL_WIDE")`，并允许
后续语义解析把执行域收敛到授权模型内的单一域。V2 Context Bridge 于 2026-09-12
之后才加入。本次新增纯 V1 回归直接验证 `department=ORG_ADMIN` 不改变该行为，证明
Bridge 修正恢复既有合同，没有重定义 V1。

## 验证结果

- A–D 合同及直接授权链：112 passed。
- Critical Suite：161 passed（冻结 160 项加本次 1 项纯 V1 合同测试）。
- 直接受影响测试对照：开发 641 passed / 2 existing failed；Git 基线
  635 passed / 同样 2 个 failed；新增 6 项全部通过，old-pass → new-fail=0。
- 全量离线对照：Git 基线 3629 passed / 92 existing failed；开发目录
  3633 passed / 94 failed。额外 2 项为任务开始前已存在且未纳入本次清单的
  `app/api.py` 标题差异；除它们外失败 node 集与 Git 基线一致。
- 模型调用、SQL、生产 Redis/Catalog 写入、服务重启和部署均为 0。

Java 平台当前没有本次曾试验的“按 semantic model 无条件补 `[205]`”实现；该试验未
提交、未部署。本次修正允许平台合法发送空 `business_domain_ids`，由 DataAnalysis
按既有 `MODEL_WIDE` 合同解析。
