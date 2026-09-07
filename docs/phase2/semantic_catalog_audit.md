# 语义目录与指标体系审计

## 读取范围

- **CURRENT_FACT**：通过 Oagnet 与生产相同的 `get_dsl_by_scope(81, 205)` 只读路径取得目录快照。
- **CURRENT_FACT**：目录包含 14 个实体、11 个指标、13 个维度、93 个属性、24 条关系。
- **CURRENT_FACT**：Milvus 健康接口显示语义集合 `knowledge_base_semantic_catalog_v1`、实体值集合 `knowledge_base_entity_values_v1`、物理目录集合 `knowledge_base_physical_catalog_v1`，向量维度 1024。
- **CURRENT_FACT**：本轮早先只读计数为语义集合 166 条、实体值集合 13,928 条；实体值以 `(model,domain,entity,attr,canonical_value)` 检查未发现重复自然键。
- **UNKNOWN**：当前本地集成环境是否与正式生产完全同一份快照；只确认私网本地服务链可读，未获得环境所有者的生产身份声明。

## 目录质量

- 11/11 指标均有定义、公式、来源依赖、绑定维度和单位。
- 5/11 指标显式配置时间锚点。
- 0/11 指标具有独立的可加性、半可加性、聚合类型字段；目前只能从公式推断，属于治理缺口。
- 13/13 维度有定义和实体绑定；只有交易日期声明日/周/月/季/年粒度，这对非时间维度属于合理现象。
- 目录声明的指标—维度兼容性已输出到 `metric_dimension_compatibility.json`，但这只是允许绑定，不等于已证明任意 Join 粒度安全。

## 角色冲突

**OBSERVED_FAILURE**：同一业务表面词在目录中可能属于多个类型，例如商品、医院、经销商、城市、省份既可作为实体也可作为维度；商品名称和交易日期同时具有属性与维度语义；数量同时可能是属性或聚合指标。若先做无类型向量召回再猜角色，会产生稳定混淆。

**PROPOSAL**：检索请求必须先带 `SemanticRole`，候选保留 canonical ID/code、mention span、来源、得分和拒绝原因；跨类型冲突必须进入 N-best 或澄清，而不是将模型原词直接写入 ASL。

## 治理建议

1. 为指标增加 `aggregation_type`、`additivity`、允许粒度、时间锚点、空值/负值/去重口径。
2. 为关系补齐经过验证的基数、方向、桥接实体及生效版本。
3. 目录发布采用 MySQL 权威版本号和 Milvus 索引版本号双写状态，查询必须回显版本。
4. 指标—维度兼容性需升级为“可用/条件可用/禁止+原因”，而不是简单绑定名单。
5. 对实体值索引维持自然键幂等，并保存 `is_deleted/source_updated_at/catalog_version`。

明细见 `semantic_catalog_inventory.json`、`catalog_quality_summary.csv`、`entity_relationship_inventory.json`。

