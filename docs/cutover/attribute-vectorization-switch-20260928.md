# 属性值向量化开关修复（2026-09-28）

## 范围与证据

- PROVEN：属性配置表 `semantic_model_attribute_config.vectorization` 表示 0=不向量化、1=向量化。现有 `/vector/entity-attributes/sync` 和 `/rebuild` 已通过完整属性源加载器检查该列，不是完全未实现。
- PROVEN（代码及离线复现）：原 `bool(flag)` 会把字符串 `"0"`、非零非法整数等当成开启；`search_mode=vector/hybrid` 能绕过关闭开关；旧名称加载函数直接读取暂存表，未应用属性配置。
- UNKNOWN：未把以上边界问题断言为当前生产已经错误入库的事实；本次未重建或审计生产索引。

## 修改清单

- `Oagnet/mysql_tool.py`：严格识别 1/True/字符串 `"1"`（允许首尾空白）；0、空值、未知值关闭；关闭开关不能被主属性、展示开关、search_mode 覆盖；审计返回采用相同判断。
- 旧 `load_entity_attribute_vector_source` 删除暂存表全量读取实现，保留作用域检查后委托统一的完整属性源加载器。
- `Oagnet/tests/test_vectorization_switch.py`：42 项新增测试，覆盖开关类型、策略优先级、业务列读取、旧入口、真实 sync/rebuild 路由到加载器/embedding/索引替换的模拟链路。

这是**属性值**索引开关，不删除属性定义或物理映射。关闭后仍可绑定字段、精确查询和展示结果。原编码/标识字段精确匹配及非文本字段不做相似度搜索的规则不变，因此配置 1 不会绕过这些原有规则；接口 `excluded_attributes.reason` 说明原因。

## 后端调用约定

- 接口请求结构不变；后端先保存配置，再调用属性值同步接口。
- 每次配置变更使用**新的 event_id**；重复使用同一 event_id 是重试，会返回同次缓存结果。
- 关闭字段从下一次成功同步起不再读取值、不生成 embedding；现有按模型/业务域替换机制删除其陈旧向量。全部关闭时不调用 embedding，按原机制返回 CLEARED 或 SKIPPED。
- 仅部署和重启不会自动清理已存在的向量。本次不修改语义库、不执行线上重建、不改变 V2 发布。

## 验证与审查

- 专项（新增开关、Milvus、属性值索引、API 集成、目录值源）：143 passed。
- Oagnet 全量：1087 passed；已有基线 1045 passed，新增 42，无新增失败或收集错误。
- DataAnalysis Critical：229 passed。未修改 DataAnalysis 功能代码或 SQL Translator。
- 人工检查：实体/属性配置仍限定原 semantic model/domain；关闭发生在业务源读取之前；保留字段定义和接口响应结构；仅同步清单文件，不包括环境配置、业务结果、日志或凭据。
- 已发布代码提交 `91a423c`。远程 mysql_tool.py 与已知版本一致，无未识别同事改动；只部署该文件并备份，未上传配置。
- 2026-09-28 14:45:55 CST 重启 Oagnet，PID 4100080；远程专项 70 passed（1 条测试依赖弃用告警），服务 UP、向量健康正常、配置哈希未变化。DataAnalysis 与 SQL Translator 未重启。
- 发布批次 `recent-91a423c-20260928-144550`；本次仅用模拟源和模拟索引验证同步路由，未触发生产索引重建。后端下一次使用新 event_id 同步时应用开关。
