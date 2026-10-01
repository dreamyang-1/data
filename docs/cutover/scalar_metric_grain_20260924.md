# 总数查询被错误扩展为医院分组：根因与修复

## PROVEN 根因

用户请求“查询上海地区的区域全部医院总数”，上游维度和展示字段为空，ASL 却出现 hospital.hospital_name，SQL 因而 GROUP BY hospital.hospital_name。这改变了查询粒度：总数变成每个医院名称组内的计数。SQL 按 ASL 翻译，此次不是 SQL 自行新增分组。

已用本地最小目录确定性复现 first divergence：

- 模型输入 ASL 的 dimensions 原本为空。
- `_normalize_semantic_references` 调用 `_normalize_explicit_grouping_dimensions`。
- `_EXPLICIT_GROUPING_NOUN` 将“全部医院总数”识别为分组量词+noun“医院总”；后续宽松实体标签匹配将其绑定到医院，补入 hospital.hospital_name。
- 相同目录和空 dimensions，“查询上海地区医院总数”不触发；“查询上海地区的区域全部医院总数”触发。新增回归在修复前 1 failed / 1 passed，足以证明代码可独立制造截图差异，无需假设模型输出随机变化。
- 原校验能确认字段来自目录，但没有阻止“不该参与本次查询的合法字段”。字段合法不代表查询粒度符合问题。界面“高可信”也不能当作意图理解正确的证据。

未读取本次线上请求日志，不宣称其模型原始输出已核实；上述代码错误路径和用户展示的 SQL 粒度错误均有直接证据。医院别名、实体编码质量仍需按目录口径另行治理，不能把 121 个名称组直接当成现实世界医院数量，也不能在输出阶段简单相加来替代修复。

## 修改边界

仅 Oagnet：

1. `agent.py`：屏蔽已选指标的目录名称/同义词内部的量词后识别分组；“所有/全部…总数”不视为逐实体分组。保留已有“所有经销商已合作医院数量”等真实跨对象分组行为。
2. `agent.py`：无权威调用方合同、非开放探索时，在最终 ASL 返回前做纯汇总粒度修复。要求上游是统计/指标查询、未指定维度或输出字段，并检查补全问题没有分组/趋势/比较/排行/明细语义。移除额外维度及仅引用被移除维度的排序，记录 REMOVE_UNREQUESTED_METRIC_GROUPING。
3. `prompt_build.py`：明确指标名称不是分组指令，实体参与查询也不等于按其名称分组。
4. `tests/test_scalar_metric_grain.py`：31 个新增用例。

不把“上游 dimensions=[]”直接当硬合同；补全问题明确要求分组时仍保留。检查未经规范化的上游参考，避免将未匹配维度误视为用户未请求。多值比较、有 HAVING、原始维度对象、显式展示字段、排行及既有合同/探索保持原路径。指标编码、目录名称、筛选值、时间范围、公式和 Semantic Scope 不变；SQL Translator、DataAnalysis 代码及质量评分未修改。

## 回归与差异

| 检查 | baseline | final |
| --- | --- | --- |
| Oagnet 全量，既有存储/数据库 Mock 运行器 | 941 passed | 972 passed |
| Oagnet 仓库严格离线运行器 | 929 passed / 12 failed | 960 passed / 同样 12 failed |
| 根因/粒度与既有 temporal_metric_invariants 专项 | 初始根因 1 failed / 1 passed | 51 passed |
| 上游 Critical Slice | — | 206 passed |

- 严格报告逐项比较：old-pass → new-fail = 0，old-fail → new-pass = 0；新增 31 项通过；无 collection error，无旧断言修改。
- 12 项失败仍是此前已记录的离线存储/数据库替身缺口，未顺带修改；见同目录 administrative_finest_default_20260924.md。
- 新增完整 main 路径组合：两种问法 × 模型有/无额外医院维度，最终均 dimensions=[]，保留真实字段/结构校验。其他测试覆盖明确分组、排行、趋势、明细、原始维度对象、时间范围和指标排序。
- Critical Slice：question_rewriter、pending_execution_transition、phase0b_critical、api、workflow、bridge_event_lifecycle_contract、bridge_event_lifecycle_wiring、asl_surface_handoff，共 206 passed。

## Review 与发布

- Review：从分组自动补全的最早分歧修复，不在 SQL 或表格末端纠正数字；不新增字段或扩大范围；显式用户粒度优先；不改冻结的节点、接口或会话合同。
- Current Stage：本项代码与离线验证完成，尚未部署或重启。本项无新增 P0/P1；线上本次请求轨迹、最终业务计数未核验。
- Catalog/Evaluation/Shadow Gap 与 V1 Replacement Readiness 不变；不代表 V2 可替代。
- Next shortest blocking path：用户要求发布后更新 Oagnet 两个运行文件，复测两种问法都返回无分组汇总，并反测各经销商/按等级/按月查询；不自动合并。
