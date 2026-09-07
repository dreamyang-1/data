# 数据智能体第二阶段语义理解与执行链路专项审计

## 0. 报告说明

| 项 | 内容 |
|---|---|
| 项目根目录 | `E:/YouoAgent/DataAnalysis_Agent` |
| 相关代码库 | `E:/YouoAgent/Oagnet`、`E:/YouoAgent/sql-translator`；辅助检查 `E:/YouoAgent/NL_Agent` |
| 检查时间 | 2026-09-07（Asia/Shanghai） |
| Git状态 | DataAnalysis源目录不是独立Git仓库；相邻本地交付仓库`E:/yy`检查时无本轮改动；按任务要求未同步、未提交 |
| 审查范围 | 多轮语义、目录/向量检索、ASL生成、SQL翻译/执行、结果契约、失败样本和评测能力 |
| 运行环境 | 本机集成开发环境；8088/8021/48000健康检查成功；环境是否与正式生产同源为UNKNOWN |
| 不可访问内容 | 30例原始完整trace与数据快照、第三方模型服务内部、生产环境身份与全量运行指标 |
| 是否修改业务代码 | 否；仅新增`docs/phase2`和隔离工具`tools/audit/phase2_materialize.py` |

标签定义：**CURRENT_FACT**为源码或只读运行直接证据；**OBSERVED_FAILURE**为真实测试/历史产物；**INFERENCE**为证据支持但尚未逐阶段复现；**PROPOSAL**为建议；**UNKNOWN**为当前证据不能回答。

## 1. 执行摘要

当前最主要错误不在单一模型，而在“对话状态 → 多轴语义 → 规范化候选 → 逻辑计划 → 结果证明”之间缺乏统一typed契约。旧intent将服务路由、分析目标和查询形态压成一个标签；ASL 1.0能完成常规查询，但比较、关系目标、通用结果契约等不是一等字段；结果层不能普遍证明请求列、粒度、顺序、数量全部满足。

对30条真实历史失败按首错阶段保守归因：结构化意图3条、槽位合并5条、ASL高概率11条、已有数据集操作5条，6条因缺原始trace而保留UNKNOWN或ASL/结果跨层不确定；**没有一条有足够证据确定SQL生成是首错阶段**。语义规范化可能参与3条UNKNOWN，但不能用当前材料强行归因。这里的计数是首错层，不是所有受影响层。

已证实：三仓源码和真实调用链、ASL真实字段、翻译器接受字段、模型调用位置、目录205的实体/指标/维度/属性/关系数量、Milvus物理集合、测试失败簇。仍未知：生产快照身份、30例完整阶段输出、业务指标可加性、关系基数是否全部正确、真实模型线上准确率/延迟/成本及多租户并发隔离。

## 2. 相关服务与代码库总览

1. DataAnalysis_Agent（8088）：会话编排、意图、状态、调用方契约、结果组装。配置证据 `app/config.py:66-94`。
2. Oagnet（8021）：按角色检索Milvus/MySQL目录、构造Prompt、生成与规范化ASL。入口 `E:/YouoAgent/Oagnet/api.py:1149-1377`。
3. sql-translator（48000）：ASL校验、SQL翻译、只读执行和部分结果契约。入口 `E:/YouoAgent/sql-translator/api_server_prod.py:153-410`。
4. MySQL：权威语义目录；Milvus：语义、实体值和物理目录向量索引。

完整健康与源码取得状态见 `external_service_inventory.json`。所有地址只保留本机端点，认证和私网完整连接信息已脱敏。

## 3. 外部ASL/NL2SQL真实调用链

**CURRENT_FACT**：DataAnalysis在 `app/adapters/http.py:1643-1673` 将问题、scope、metric IDs、调用方契约和分析契约发给Oagnet；Oagnet在 `agent.py:6871-7163` 检索候选并调用模型；DataAnalysis在 `http.py:1930-1950` 将ASL发给翻译接口，再在 `2033-2081` 调用SQL执行。

Oagnet正常路径是一次对话模型调用，只有JSON格式错误重试一次；之后进行确定性规范化、目录回填、歧义检查和调用方契约约束。SQL翻译器内部没有LLM。实际链上找到6个对话模型调用点：第一阶段已记录的DataAnalysis 5个，加Oagnet ASL生成1个；embedding依赖另计，不混作对话模型调用。

## 4. ASL与SQL契约能力

ASL真实一等操作/字段共10类：`subject`、`metrics`、`dimensions`、`filters`、`time_context`、`sort`、`limit`、`having`、`projection_mode`、`ambiguity`，证据 `E:/YouoAgent/Oagnet/prompt_build.py:10-180`。翻译流程见 `sql_translator_prod.py:2860-3186`。

多指标、时间粒度、排序和limit可表达；关系Join由目录路径推断。比较、窗口、offset/cursor、显式Join和通用ResultContract不是一等ASL。1:N风险在 `2940-2969` 被拒绝而非预聚合修复。详见 `asl_nl2sql_contract_audit.md`、`asl_nl2sql_gap_matrix.csv`。

## 5. 语义目录与指标治理现状

**CURRENT_FACT**：只读读取model 81/domain 205得到14实体、11指标、13维度、93属性、24关系。11个指标均有定义、公式、来源、绑定维度和单位，5个有时间锚点，0个有独立可加性字段。

Milvus数据库为`knowledge_base`，语义集合`knowledge_base_semantic_catalog_v1`，实体值集合`knowledge_base_entity_values_v1`，物理目录集合`knowledge_base_physical_catalog_v1`。只读检查时语义166条、实体值13,928条；按规范自然键没有重复。数据库/集合细节在 `semantic_catalog_inventory.json`，治理结论见 `semantic_catalog_audit.md`。

## 6. 指标、维度、实体、字段角色解析现状

**CURRENT_FACT**：Oagnet按entity、attribute、relation、metric、dimension、entity_attribute_value分类型召回，而非把所有文本混在一类。**OBSERVED_FAILURE**：目录中仍有同表面词跨角色：商品、医院、经销商、城市、省份可同时是实体与维度；商品名称、交易日期可在属性与维度语义间切换；数量可指属性或聚合指标。

目标应先产出mention span与SemanticRole，再在该role内检索canonical ID；未命中不得进入执行计划，模型原词只保留为provenance。冲突证据见 `semantic_role_confusion_matrix.csv`。

## 7. Milvus候选召回和消歧现状

类型/作用域过滤位于 `Oagnet/prompt_build.py:394-475`；精确提及优先和向量重排位于 `568-621`；每类型候选召回位于 `984-1149`；关系图最大4跳补全位于 `807-982`。

当前没有BM25；调用方绑定的metric可精确加载。候选池没有完整记录“每个候选为何被拒”，跨类型歧义缺统一守卫。**PROPOSAL**：返回role-scoped N-best、分数、版本、字段归属、关系可达性和拒绝原因。详见 `semantic_resolution_pipeline.md`、`semantic_candidate_examples.jsonl`。

## 8. 30个失败案例逐阶段归因

30/30完成历史证据局部重放归类，0条具备完整在线重放条件；24条能定位高概率首错阶段，6条保留UNKNOWN/跨层。不能重放的原因是历史产物没有结构化history、state、候选池、原始模型响应、ASL、SQL和数据快照。

主要簇为槽位ADD合并、明细/指标形态、时间粒度/关系投影、已有数据集limit/sort。逐例材料见 `failure_trace_replay.jsonl`，分布见 `failure_stage_distribution.csv` 和 `failure_root_cause_summary.md`。

## 9. 能力实现矩阵

常规多指标、目录Join和只读SQL已实现；追问合并、时间语义、实体值规范化、排序追问、比较和ResultContract为部分实现；完整Trace、多租户并发隔离和自动1:N预聚合未完成或UNKNOWN。详见 `capability_implementation_matrix.md`。

## 10. 当前意图体系混淆分析

**OBSERVED_FAILURE**：单一intent无法稳定区分“这是追问还是新问题”“重跑语义查询还是处理已有数据集”“做何种分析”和“返回何种形态”。扩展测试18项失败进一步集中在澄清grounding、PendingState和中断恢复。

推荐将旧intent降为派生兼容字段；详细证据见 `intent_confusion_analysis.md`、`intent_confusion_matrix.csv`、`slot_role_confusion_matrix.csv`。

## 11. 多轴意图Schema可行性

建议五轴：DialogueAct、ServiceRoute、AnalysisGoal[]、QueryShape、SemanticRole。它们可由现有字段shadow生成，不需要首先改变SQL执行。REFRESH与REVISE必须保留为独立DialogueAct：前者语义不变、后者生成新任务版本，普通自然语言修改仍走FOLLOW_UP/CORRECTION判定。

可行性为**HIGH**，但业务标签边界与验收阈值仍需金标确认。

## 12. Typed Logical Plan可行性

`typed_logical_plan_schema_draft.json`定义subject、typed projections、groupings、filters、统一time、sort/limit、comparison、result contract与provenance。目标计划先由适配器降级成ASL 1.0，shadow验证等价性；不能把草案直接当生产Schema。映射见 `current_to_target_schema_mapping.md`。

## 13. 指标语义代数可行性

现有公式提供良好基础，但必须显式增加aggregation、distinct key、additivity、allowed grains、time anchor、source grain和空值/负值规则。比率必须聚合分子分母后再计算，不能对行级比率求和。详见 `metric_algebra_feasibility.md`。

## 14. 时间语义可行性

统一时间对象应包含anchor/range/grain/boundary/timezone/calendar/source/as_of。查询区间超过数据水位时应解释并让用户选择，不得静默改日期。当前5/11指标有时间锚点，历史9例与时间粒度/投影有关。详见 `temporal_semantics_feasibility.md`。

## 15. ResultContract可行性

当前Dataset验证位于 `app/domain/models.py:746-808`；分析契约位于 `sql-translator/analysis_contract.py:19-160`。现有校验适合扩展，但缺少通用grain、required columns、ordering、row bounds和relation cardinality proof。

**PROPOSAL**：ResultContract由计划生成，不由结果事后猜；SQL翻译前验证可满足性，执行后验证证据。失败应区分EMPTY_VALID、CONTRACT_VIOLATION、UPSTREAM_UNAVAILABLE和DATA_STALENESS。

## 16. 当前测试和评测缺口

本轮实际执行：DataAnalysis核心242通过/7失败，扩展725通过/18失败，Oagnet 185通过，SQL Translator 101通过，总计1253通过、25失败、0跳过；另2个模块因旧Schema符号缺失收集失败。完整命令边界见 `test_execution_report.md`。

200条现有旧intent断言已转换为部分金标种子，明确不是多轴完整金标；synthetic proposal另文件隔离。新指标定义见 `evaluation_metric_spec.json` 和 `evaluation_framework_gap.md`。

## 17. 集成点和迁移方式

阶段0目录/Trace基线；阶段1多轴解析和Slot Reducer shadow；阶段2角色优先N-best与Typed Plan shadow；阶段3ASL/SQL扩展和ResultContract；阶段4按域灰度与旧链退役。每阶段均保留版本开关和旧路径回滚。详见 `integration_and_migration_map.md`。

## 18. 跨仓库改动矩阵

DataAnalysis负责对话与typed plan入口；Oagnet负责角色候选、规范化和plan adapter；SQL Translator负责强Schema、grain安全和结果证明；目录负责稳定ID、指标代数和关系基数；Milvus负责版本化索引及候选证据。详见 `cross_repository_change_matrix.csv`。

## 19. 风险清单

| ID | 优先级 | 风险 | 所属层 | 触发条件 | 影响 | 证据 | 建议 |
|---|---|---|---|---|---|---|---|
| R01 | P0 | ADD/REPLACE槽位错误 | STATE | 多轮修改 | 错指标/筛选 | real-001等5例 | typed reducer+task version |
| R02 | P0 | 查询形态被单intent掩盖 | INTENT | 明细/统计边界 | 错路由 | real-002/005/022 | 多轴意图 |
| R03 | P0 | 时间粒度未进计划 | ASL | 按月/按年 | 错粒度 | 9条历史案例 | 统一time对象 |
| R04 | P0 | 请求字段未被结果证明 | RESULT_VALIDATION | 多列/排名 | 看似成功但漏列 | real-003 | 通用ResultContract |
| R05 | P0 | 指标可加性未知 | SEMANTIC_CATALOG | 复杂Join/比率 | 聚合失真 | 0/11显式可加性 | 指标代数治理 |
| R06 | P1 | 跨类型表面词冲突 | SEMANTIC_RESOLUTION | 商品/城市等 | 错规范项 | 角色冲突矩阵 | role-first N-best |
| R07 | P1 | 1:N只拒绝不修复 | SQL | 多关系聚合 | 查询失败 | translator:2940-2969 | 受控预聚合 |
| R08 | P0 | Trace不足无法首错定位 | OBSERVABILITY | 线上失败 | 修复靠猜 | 30例均缺完整trace | 全阶段事件契约 |
| R09 | P1 | 测试与Schema漂移 | EVALUATION | 模型重命名 | 回归失真 | 2个收集错误 | 测试契约门禁 |
| R10 | P0 | 环境身份未证实 | SECURITY | 使用私网服务 | 审计边界不清 | 本轮UNKNOWN | 环境标识与只读账号 |
| R11 | P1 | 多租户隔离未压测 | STATE | 并发同会话键 | 状态串扰 | 无并发证据 | 隔离属性组合测试 |
| R12 | P1 | 相对时间晚于数据水位 | LOGICAL_PLAN | “最近一年” | 合法空结果 | 当前截图类问题 | 水位解释与用户选择 |

## 20. 对最终方案的修正建议

- 保留第一阶段：短期记忆分层、PendingState、刷新/修改重提区分、向量规范化优先、完整Trace和回归体系方向。
- 需要修正：不要把问题归结为“换模型”或仅改Prompt；不要让typed plan直接替换ASL；不要把所有目录类型放进一个无角色候选池；不要自动改用户时间范围。
- 新增：多轴意图、mention span、SemanticRole、N-best与拒绝原因、Slot Reducer、Task Version、指标代数、统一时间对象、ResultContract、目录/索引版本。
- 证据不足：生产候选命中率、指标逐项可加性、完整关系基数、在线模型收益、真实成本/延迟和多租户隔离。

## 21. 推荐实施顺序

阶段0先完成目录/Trace基线，因为后续所有差异必须能绑定目录和任务版本；验收为快照可复现、全阶段trace可回放，回滚为停用新增元数据/事件。阶段1做多轴与Reducer shadow，验收多轮金标且不改变执行，回滚关开关。阶段2做角色候选和Typed Plan shadow，依赖稳定ID，验收TopK与等价率，回滚旧ASL。阶段3扩展SQL和ResultContract，依赖稳定计划，验收Join/粒度/结果证明，回滚ASL1.0。阶段4灰度切流，依赖质量/性能/隔离门禁，按业务域立即回切。

## 22. 仍需业务方确认的问题

1. 11个指标各自的可加性、去重键、退货/冲销、空值和允许粒度。
2. “合作”“有效医院”“主要/次要科室”等关系的业务基数和生效规则。
3. 自然周、财年、数据水位之后区间、缺月补零的展示口径。
4. 多候选何时自动选Top1、何时必须追问的风险阈值。
5. 正式生产环境标识、数据保存期限、第三方模型可见数据边界。
6. 各项新评测的上线门槛、可接受延迟与成本预算。

## 23. 给架构师的最终材料索引

- 真实调用与字段：`external_service_inventory.json`、`asl_nl2sql_contracts.json`、`asl_nl2sql_contract_audit.md`。
- 目录证据：`semantic_catalog_inventory.json`、`metric_dimension_compatibility.json`、`entity_relationship_inventory.json`。
- 失败证据：`failure_trace_replay.jsonl`、`failure_stage_distribution.csv`。
- 目标Schema：`typed_logical_plan_schema_draft.json`、`current_to_target_schema_mapping.md`。
- 迁移与责任：`integration_and_migration_map.md`、`cross_repository_change_matrix.csv`。
- 金标与评测：`gold_cases_seed.jsonl`、`synthetic_test_proposals.jsonl`、`evaluation_metric_spec.json`。
- 方案—证据映射：`architecture_evidence_map.json`。
- 关键源码：`app/services/intent_asl_contract.py:62-202`、`app/adapters/http.py:1643-2183`、`Oagnet/prompt_build.py:394-1149`、`Oagnet/agent.py:6871-7163`、`sql-translator/sql_translator_prod.py:2860-3186`。

