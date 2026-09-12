# V2 Demo Accuracy Sprint R3

**DEMO_ACCURACY_SPRINT_R3 = PARTIAL**

确定性语义 Grounding 已接入真实 V2 RawTurnPlanner：CurrentTurn 之后先尝试用当前显式证据、固定 Catalog 和精确 Source Value 证明构造完整 `SemanticTaskDraft`；只有全部条件满足才跳过 SemanticEdits，任何不完整、歧义或复杂语义都回到原路径。V1、Prompt、模型 Schema、Scope、Reducer、API/SSE、Oagnet、SQL Translator 和 8088 均未修改。

## 结果

| Gate | 状态 |
|---|---|
| Offline regression | **273 passed / 0 failed / 0 collection errors** |
| Smoke15 initial | **5/15** |
| Smoke15 after the only generic fix loop | **7/15** |
| Smoke minimum 12/15 | **FAIL** |
| Core50 | **NOT_RUN_BY_SMOKE_GATE** |
| V1 baseline | **15/37，未重跑** |
| Core50 First Task Publication | **未重测；保留基线 2/30** |
| Smoke First Task Publication | **4/13 → 8/13** |
| 8088 | **NOT_READY / NOT_RUN** |

## 修改与原因

新增 `deterministic_grounding.py`，覆盖限定的标量、分组、TopN、精确实体值、显式指标/维度以及已有 Task 上的简单安全编辑。接入点位于 CurrentTurn/Context/Candidate 之后、SemanticEdits 之前；最终仍由既有 Source Value、TaskPatch、Reducer、Payload、Scope 和 Catalog 校验发布。

唯一通用修复循环定位并修正了真实动态 Schema 对齐缺陷：CurrentTurn 会返回全部 slot 键，未使用项为空数组，初版却把空的未知 slot 也当成已声明能力，导致真实 Fast Path 大量误回退。Fix1 只检查非空 slot。同期补全两项目录证明规则：指标/维度可使用唯一最长的正式名称或目录同义词；Ranking subject 可以从同一明确维度的唯一 `bind_entities` 归属得到。实体类型仍要求整词精确，具体实例仍要求 Source Value 精确证明。

这些修改是可回滚的独立路径：构造函数参数可以关闭 fast path；失败时不产生部分 Draft、不修改 Task，直接进入原 SemanticEdits。新增 TopN 正则 1 个；业务词特判、Case ID、Gold 原句、Catalog ID、Prompt 规则和 confidence 常数增量均为 0。

## 安全边界

- Scope 81/[205]、Catalog Pin、Source Value 证明和现有 Guard 全部保留。
- `NEW_TASK` 不继承历史；Pending、历史返回、复杂关系、比较和 Dataset 不进入 fast path。
- 实体名称不做包含匹配，避免把具体实例降成泛化实体。
- 指标/维度只接受目录正式词的唯一最长匹配；并列最优、无匹配或多归属均回退。
- Source Value 无证明或多字段命中时回退；没有模糊值绑定。
- CLEAR/REMOVE、复杂时间和原有状态不变量继续由既有路径处理。

## 验证

17 项新增/专项测试覆盖：精确商品、医院、省份、泛化实体、分组、多指标、TopN 产品/经销商、新任务隔离、缺失/歧义证明、具体实体替换回退、关系回退、Pending、时间追问、指标 ADD、空 slot 和非空不支持 slot。包含 Round2.3/2.4 Entity、Source Value、Recognition、Context Critical Slice、A–E、completed question 和 Canonical bridge 的受影响集合最终为 **273/273**。

Fix1 Smoke 共 37 轮、58 次模型请求：CurrentTurn 37、SemanticEdits 21，HTTP 均成功；确定性路径发布 14 轮并避免 14 次 SemanticEdits。只读 Source Value 观察 198 次；业务聚合 SQL、V1 调用、生产写入、Redis、ASL、8088 调用均为 0。

## 停止点

Smoke 严格分数未达到 12/15，且唯一允许的通用修复循环已经使用。依指令不运行 Core50，不创建 `CORE50_RESULT.md`，不运行 8088，也不创建 `8088_DEMO_RESULT.md`。当前最高阻塞是剩余真实多轮编辑/时间合同和具体实体替换仍依赖不稳定的 SemanticEdits；另有三条 Smoke 标签需要未来独立裁决，但即使裁决通过也不足以打开 Core50 Gate。

基线为 `7fa2d0bd5e6ab63ebda773751c7aca3f3bafa4a8` / Draft PR #75；开发分支为 `v2-demo-accuracy-sprint-r3-20260912T043720Z`。本分支只交付上述最小生产路径、测试和两份公开摘要，不合并、不切换 V1。
