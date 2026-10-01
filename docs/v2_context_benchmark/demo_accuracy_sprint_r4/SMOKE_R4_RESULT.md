# Demo Accuracy Smoke15 — R4

**SMOKE_R4_GATE = FAIL**

本次使用与 R3 完全相同的冻结 Smoke15，数据集 SHA-256 为 `b4f51942de6d017b1541721dfcf9fb2796c15b57040c7146b7c4ba222b015ba3`。只运行一次 R4；无 Harness 重试。原 Strict 7/15 永久保留，本次结果为 Strict **8/15**、Business Reviewed **10/15**。

| Case | Strict | Reviewed | 结果 |
|---|---|---|---|
| RB50-02 | PASS | PASS | 省份分组订单笔数 |
| RB50-04 | FAIL | AMBIGUOUS | 医院数量与已合作医院数口径未证明一致 |
| RB50-06 | FAIL | PASS | 原句明确要求的医院等级被旧期待遗漏 |
| RB50-08 | PASS | PASS | 四川省含税销售总额 |
| RB50-09 | FAIL | PASS | 发布指定医院的正式 `hospital_affiliated_department_count` 计划 |
| RB50-13 | PASS | PASS | 保留商品条件，替换为销售总数量 |
| RB50-14 | PASS | PASS | 保留商品条件，替换为订单笔数 |
| RB50-17 | PASS | PASS | 保留商品条件，按城市分组 |
| RB50-18 | FAIL | FAIL | `V2_TEMPORAL_BASE_REQUIRED`；没有时间基线 |
| RB50-29 | PASS | PASS | 精确替换已有商品 Filter，未调用 SemanticEdits |
| RB50-31 | FAIL | FAIL | `V2_SLOT_OPERATION_CONFLICT`；没有满足唯一 CLEAR 证据 |
| RB50-32 | PASS | PASS | 经销商 Top5 |
| RB50-36 | FAIL | FAIL | 本次 CurrentTurn 后返回 `V2_SLOT_OPERATION_CONFLICT`，没有发布计划 |
| RB50-37 | PASS | PASS | 具体商品条件与含税销售总额 |
| RB50-40 | FAIL | FAIL | `V2_MODEL_DYNAMIC_SCHEMA_VIOLATION`；无精确实例证明 |

R4 共执行13条独立链、37轮。First Task Publication 为 **8/13**；Fast Path 命中17轮，比 R3 的14轮增加3；SemanticEdits 调用17次，比 R3 的21次减少4。54次模型调用均 HTTP 200，CurrentTurn 37次、SemanticEdits 17次，retry=0；输入694,709 tokens、输出27,021 tokens、总计721,730 tokens。精确只读 Source Value 观察213次，业务聚合 SQL、V1、Redis、生产写入和8088调用均为0。

Smoke结果 SHA-256 为 `832957b9455ad6167642c0fa1ffdc2fdbcd082f1b71a6c8b355bdad7b2d2b388`。原始运行证据保存在 PRIVATE，不进入 Git。由于 Reviewed 10/15 未达到12/15，Core50和8088均不运行。
