# 语义检索、规范化与消歧链路

## 当前链路

```text
用户问题
  → DataAnalysis 结构化意图/调用方契约
  → Oagnet 按语义类型分别向量召回
  → 精确提及优先 + 向量分数重排
  → 关系图最短路径补全（最大4跳）
  → 大模型生成 ASL
  → 确定性规范化、调用方契约回填、歧义检查
  → SQL 翻译与目录校验
```

## 证据

- 类型与作用域过滤：`E:/YouoAgent/Oagnet/prompt_build.py:394-475`。
- 精确提及优先和去重重排：`prompt_build.py:568-621`。
- 关系图补全：`prompt_build.py:807-982`。
- entity/attribute/relation/metric/dimension/entity_attribute_value 分类型召回：`prompt_build.py:984-1149`。
- 同类型候选歧义检查：`E:/YouoAgent/Oagnet/agent.py:6681-6853`。

## 发现

- **CURRENT_FACT**：不是单纯靠大模型抽取；目录候选与调用方强绑定指标会参与 ASL 生成和后置规范化。
- **CURRENT_FACT**：检索是向量+精确提及重排，不是 BM25 混合检索。
- **CURRENT_FACT**：预重排候选池可进入内部歧义信息，但没有结构化记录每个淘汰候选的拒绝原因。
- **OBSERVED_FAILURE**：现有歧义守卫主要处理“同一短语的同类型多个规范项”，跨类型冲突没有统一 typed role 决策。
- **INFERENCE**：历史的商品/经销商/城市等错位，与角色晚绑定和关系目标非一等表达一致；由于失败样本缺少原始候选 Trace，不能逐例证明具体候选被如何淘汰。

## 目标链路

先识别 mention span 和候选角色，再按 `(scope, role, span)` 召回；输出 N-best 候选及证据，由确定性约束器结合查询形态、字段归属、关系可达性和兼容矩阵选解。仅在两个可执行计划仍不可区分时追问用户。选中的规范项必须完全替换模型原始值，模型文本只留作 provenance。

