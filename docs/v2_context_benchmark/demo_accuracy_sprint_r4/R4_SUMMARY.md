# V2 Demo Accuracy Sprint R4

**DEMO_ACCURACY_SPRINT_R4 = PARTIAL**

R4 在 R3 提交 `8b8ea76ed8d8fe579f4b809499aa471a0d23d5a4` 上完成两项高收益通用修复和同一批 Smoke15 的一次复测。严格分数由 7/15 提升为 **8/15**，业务复核分数为 **10/15**，仍未达到 Core50 门槛；因此 Core50、V1、8088、ASL 和业务聚合 SQL 均未运行。

| 项目 | 结果 |
|---|---:|
| Strict Smoke | **8/15** |
| Business Reviewed Smoke | **10/15** |
| First Task Publication | **8/13** |
| Fast Path Hits | **17/37 turns** |
| SemanticEdits Calls | **17** |
| 模型调用 | 54，全部 HTTP 200，retry=0 |
| Tokens | 721,730 |
| Core50 | **NOT_RUN_BY_SMOKE_GATE** |
| V1 baseline | 15/37，未重跑 |
| 8088 | **NOT_READY / NOT_RUN** |

## 独立评测复核

- `RB50-06`：`BUSINESS_CORRECT_EVALUATOR_MISMATCH`。原句明确要求关联医院等级；实际计划保留医院、医院等级、含税销售总额和订单笔数。
- `RB50-36`：R3 输出中的“产品”与正式目录维度“商品”是同一 governed synonym，R3 结果本身正确；R4 新运行在发布前返回 `V2_SLOT_OPERATION_CONFLICT`，所以不能把旧结果计入 R4 reviewed score。
- `RB50-04`：`AMBIGUOUS_REQUIRES_REVIEW`。“医院数量”与目录“已合作医院数”的统计总体没有证明相同，未补记通过。
- `RB50-09`：R4 从拒绝恢复为完整计划。用户要求指定医院的科室数量，实际绑定正式指标 `hospital_affiliated_department_count / 医院下属科室数量`；冻结期待“科室总数量”不是模型81/域205中的正式指标，故只计入 Business Reviewed，不改 Strict Gold。

## Failure Cluster 与修改

`RB50-09/29/40` 的相同动态 Schema 错误码来自不同前置缺口，结论为 `DYNAMIC_SCHEMA_FAILURE_CLUSTER = MULTIPLE_ROOT_CAUSES`。本轮没有放宽 Dynamic Schema。

1. 指标短表达只有在全部已提供目录候选中唯一时，才允许反向包含匹配；有多个候选立即回退。该路径恢复 `RB50-09` 的首任务发布。
2. 已有 Filter 的 REPLACE 只有在当前 Predicate 字段上获得唯一精确 Source Value 证明时才执行；CLEAR 只可删除唯一的非主体 Predicate。该路径恢复 `RB50-29`，并保留 `RB50-31` 的安全拒绝。

生产增量没有新增业务词、Case ID、Gold 原句、Prompt、模型阶段、confidence 或模糊值匹配。缺少 TimeSpec 的 `RB50-18` 继续拒绝；无精确实例证明的 `RB50-40` 继续拒绝。

## Offline Gate 与停止点

专项测试 **21 passed / 0 failed**。覆盖 Recognition、Source Value、Entity preservation、Context Critical Slice、A–E、completed question 和 Canonical 的清洁门禁为 **270 passed / 0 failed**。完整选中集合另有3个 R3 基线已存在的旧失败；基线为266 passed /3 failed，当前为270 passed /3 failed，新增4项通过，`old-pass -> new-fail = 0`。

Smoke 的 Strict 8/15 与 Reviewed 10/15 均低于12/15，所以不运行 Core50；也不满足 8088 Demo Trial 门槛。本轮到此停止，不启动 R5，不进行 cutover、canary 或 V1 replacement。
