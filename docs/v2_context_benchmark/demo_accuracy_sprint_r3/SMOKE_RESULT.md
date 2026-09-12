# Demo Accuracy Smoke 15 — R3

**SMOKE_GATE = FAIL（严格冻结评分 7/15）**

本轮从冻结 Core50 / `实际业务问题.md` 选出 15 条，问题、历史和期待在模型调用前写入 PRIVATE 并固定。初始运行严格得分 5/15；唯一一次通用修复循环后为 7/15，仍低于最低门槛 12/15。因此没有运行 Core50、V1、8088、ASL 或业务聚合 SQL。

| 项目 | 初始运行 | Fix1 |
|---|---:|---:|
| 严格 PASS | 5/15 | **7/15** |
| 13 条独立链首轮 Task 发布 | 4/13 | **8/13** |
| 目标轮确定性 Fast Path | 0 | **9** |
| 全部 37 轮确定性 Fast Path | 3 | **14** |
| CurrentTurn 调用 | 38 | 37 |
| SemanticEdits 调用 | 27 | **21** |
| 总模型调用 | 65 | 58 |
| 精确只读 Source Value 观察 | 79 | 198 |

初始运行的 CurrentTurn 为 38 而非 37，是因为第一条 Source Value 读取遇到 PRIVATE Runner 的 Oagnet 模块路径缺口。该次失败立即中止并单独保留，修复 Runner 后只重试这条基础设施失败；旧尝试和模型调用均计入数字。Fix1 没有 Harness 失败。

## Fix1 严格结果

| Case | 结果 | 首个未满足项 |
|---|---|---|
| RB50-02 | PASS | — |
| RB50-04 | FAIL | 冻结期待指标名与目录正式指标不一致 |
| RB50-06 | FAIL | 冻结期待未包含原句明确要求关联的医院等级 |
| RB50-08 | PASS | — |
| RB50-09 | FAIL | `V2_MODEL_DYNAMIC_SCHEMA_VIOLATION` |
| RB50-13 | PASS | — |
| RB50-14 | PASS | — |
| RB50-17 | PASS | — |
| RB50-18 | FAIL | `V2_TEMPORAL_BASE_REQUIRED` |
| RB50-29 | FAIL | `V2_MODEL_DYNAMIC_SCHEMA_VIOLATION` |
| RB50-31 | FAIL | `V2_SLOT_OPERATION_CONFLICT` |
| RB50-32 | PASS | — |
| RB50-36 | FAIL | 冻结期待使用“产品”，计划使用目录正式名称“商品” |
| RB50-37 | PASS | — |
| RB50-40 | FAIL | `V2_MODEL_DYNAMIC_SCHEMA_VIOLATION` |

RB50-04、RB50-06、RB50-36 是 **SMOKE_EVALUATOR_REVIEW_CANDIDATE**：RB50-04 的输出与原 Core50 已接受的目录指标一致；RB50-06 保留了用户明确提出的医院等级；RB50-36 使用“产品”的目录正式同义词和规范名“商品”。这些证据在看到结果后才完成交叉复核，所以本轮没有修改已冻结期待，也没有把三条补记为 PASS。即使全部经独立裁决通过，观察上限也只是 10/15，仍低于 12/15。

## 确定性路径证据

Fix1 的 14 个已发布轮次只调用 CurrentTurn，没有调用 SemanticEdits。它们包括两个 Top5、精确省份、三个精确商品首问、指标追问、城市分组追问和独立新话题。SemanticEdits 模型调用因此避免 14 次。所有 Source Value 都经过 scope 81/[205]、冻结 Catalog Pin、正式字段映射和实时精确只读证明；没有持久化业务值到 Git。

复杂关系、比较、未证明值、时间基线缺失和动态 Schema 违规仍走原 SemanticEdits 或原 fail-closed 守卫。没有把拒绝计作通过，没有伪造前置 Task，也没有用新任务继承旧状态。

PRIVATE 证据：`.eval_private/demo-accuracy-sprint/`。冻结数据 SHA-256 为 `b4f51942de6d017b1541721dfcf9fb2796c15b57040c7146b7c4ba222b015ba3`；初始结果 SHA-256 为 `45b8b2f2ad7210d36dd3aa46dcdc5d2160bbe29b93c3995bec181490950483e6`；Fix1 结果 SHA-256 为 `ce06f79a1f99624b1759a483c6fb0c7742f67e18b9b0a5905375f8e16fe48404`。PRIVATE 原始输出不进入 Git。
