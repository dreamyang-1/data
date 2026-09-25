# ASL / SQL 容错审查与最小修改

## 范围与决策

用户明确要求降低不规范配置导致的误拦截。本轮审查 ASL 生成后处理、
最终验证、SQL 翻译入口、单域计划入口、指标固定筛选解析和只读执行边界。
不修改 DataAnalysis 编排、不改变节点顺序、不修改目录和业务数据。

## 已证实并处理（PROVEN）

| 原行为 | 新行为 |
| --- | --- |
| 可选列表为 null 时拒绝或进入迭代异常 | 统一为空列表 |
| 完全重复的指标/维度导致拒绝或重复列 | 精确去重；不同粒度/参数不合并 |
| 版本 2.0 为数字、枚举大小写/外侧空格不一致 | 转为标准表达 |
| 正整数行数以字符串传递时拒绝 | 转为整数后仍检查上下限 |
| 物理维度重复填写相同列名 attr | 清除冗余 attr；不同字段仍拒绝 |
| 指标固定筛选是单个对象而非数组时拒绝 | 包装为数组，保留所有条件 |
| 固定筛选类型 INCLUDE / Exclude 等大小写不同 | 大小写与外侧空格归一化 |
| 固定筛选目录列为空字符串 | 按无固定筛选处理 |

规范化必须在计划和后处理前执行，而不只是最终校验前执行；已经覆盖普通
translate、translate_only 和 ScopedTranslator.translate_only 入口。

## 保留的校验

- 模型/业务域/数据源范围、会话隔离、只读操作及参数绑定。
- 字段/指标绑定、必需筛选、明确时间范围、公式存在性、关联路径。
- 同名但不同含义的维度/指标冲突，以及未解决的业务歧义。
- 未知 include/exclude 类型和坏的固定筛选条件：不能猜正反逻辑，不能删条件。
- LIMIT 上限、危险 SQL、跨源执行限制。

这些条件不满足时，放行可能改变查询对象、金额或权限，而不只是容忍配置格式。

## 其他审查发现（本次未扩大修改）

- PROVEN：_select_surface_mention_match 对剩余同分候选使用 random.choice，
  存在绑定不稳定风险；不应通过删校验来掩盖。当前没有新增线上复现证明其
  具体业务影响，未顺带改变已运行的匹配策略。
- PROVEN：advisory mention 的未命中分支可删除对应草稿筛选；向量/源数据不全
  时可能扩大查询。用户此前要求参考项未命中丢弃，此次不擅自反转该合同。
- PROVEN：只读校验拒绝多数子查询，同时作用域表解析对复杂 SQL 有限制；
  单独删子查询检查不能保证复杂查询可正确执行，因此本次保留。
- UNKNOWN：仍有哪些线上拒绝来自目录缺字段、缺关联或缺公式。
  本轮离线审查不能把这些统称为误拦截，也不宣称已经核对销售结果。

## 文件与测试

- Oagnet：agent.py；test_hardening.py；新增 test_asl_compatibility.py。
- SQL Translator：sql_translator_prod.py、semantic_scope.py；
  test_metric_global_filter_normalization.py；新增 test_asl_compatibility.py。
- Oagnet 原 759 项中两项重复项拒绝用例按本次决定转为正例，新增七项用例：
  最终 764 passed，collection errors=0。
- SQL Translator 原 446 项中大小写拒绝的旧断言更新（STALE_TEST），
  保留未知类型的反例，新增九项：最终 455 passed，collection errors=0。
- 未改安全、Scope、时间范围或金额口径的负例。无其他新增失败。
- 本次未部署或重启线上服务；发布需要同步对应服务明确文件清单。

本轮是 V1 容错修复，不是 V2 切流；不改变 V2 replacement readiness。
