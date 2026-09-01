# Long-range Conversational Data Agent V2 设计报告

> 文档类型：架构研究、现状扫描、差距分析与实施方案  
> 当前阶段：仅设计，不修改运行代码  
> 研究与扫描日期：2026-09-01  
> 适用项目：`DataAnalysis_Agent`

## 执行摘要

当前系统已经具备较成熟的数据查询、确定性分析、短期追问、待补充状态、结果数据集复用、长期偏好记忆、事件审计和任务 DAG 能力。它不是“没有记忆”，而是现有记忆主要围绕**当前任务和最近分支**组织：一个会话维护一个活跃 `task_frame`、至多 12 个近期任务帧、至多若干近期数据集引用，以及由调用方传入的有限历史消息。

这足以处理“上一轮 → 下一轮”，但不足以稳定处理“20 轮后回到最开始那个分析”。根因不是提示词不够长，而是缺少以下中间层：

- 可持久、可检索的 Conversation Event Store；
- 将连续业务主题聚合起来的 Analysis Thread；
- 保存重要分析快照的 Analysis Episode；
- 面向历史主题、结果集、序数和时间指代的 Reference Resolver；
- Local Recall 与 Global Recall 分级召回；
- 历史状态快照恢复和当前增量合并；
- 结果数据集的分支、根版本和父版本选择策略。

因此，V2 不应重写现有 Agent，也不应引入完整 Letta、Graphiti 或 Neo4j。推荐在现有 Python + FastAPI + LangGraph + Redis + MySQL + MinIO 架构上增量增加“会话历史检索平面”，并把当前 State 明确限定为**当前 Active Thread 的运行态**。

截图问题的直接结论也很明确：原始 22 条结果为 A0，“只展示前五名”生成 A1，且 A1 的 `parent_dataset_ids=(A0)`；后续“展示前十名”仍自动选择最近的 A1，所以对 5 行再次取前 10，最终仍是 5 行。“最开始的 22 条里面的前十条”同样失败，是因为当前选择器不会理解“最开始”，也不会沿父链回到 A0。数据血缘已经存在，缺的是**历史结果引用解析与祖先版本选择**。

---

## 1. 当前实现

### 1.1 当前多轮会话完整执行链

当前主链可以概括为：

```text
FastAPI 请求与身份校验
  -> 恢复 Pending / 当前 Task Frame / Last Request
  -> 可选：从最近 12 个 Task Frame 做显式词法召回
  -> Question Rewrite（基于一个 previous request）
  -> 意图与结构化参数识别
  -> Pending Clarification 合并或新任务替换判断
  -> 文件感知
  -> 任务拆分与 DAG 规划
  -> 语义查询规划 / ASL / 只读 SQL
  -> SQL 或工具执行
  -> 查询结果持久化为 MinIO DatasetReference
  -> 确定性 Python Analysis / Insight 表达
  -> 结果校验、Evidence、Reliability
  -> SSE 输出
  -> 保存 Last Request、Task Frame、事件、可选长期偏好与 Trace Summary
```

`app/graph/workflow.py` 定义了 24 个有名称的 LangGraph 节点，但核心业务仍由 `skill_dispatch` 内部调用现有 Orchestrator 完成；不少图节点承担阶段标记和可观测性职责。当前使用 `graph.compile()`，没有传入 LangGraph checkpointer，所以 LangGraph State 本身不是跨请求的持久会话存储。真正的跨请求恢复由自建 `SessionStore`、Pending CAS、DAG checkpoint 和 MinIO DatasetReference 承担。

### 1.2 当前 State 保存什么

`app/graph/state.py::AgentState` 保存单次运行所需的丰富工作态，包括：

- 原始 ChatRequest、受信身份和应用范围；
- conversation control、canonical request、pending task；
- intent candidates、semantic context snapshot、semantic plan evidence；
- task plan、task executions、ASL、SQL/query result；
- dataset 与 dataset reference；
- analysis facts、insights、narrative、evidence、reliability；
- response、trace、audit events。

这些字段适合作为一次运行或一个 Active Thread 的工作状态，不适合作为整个 Conversation 的长期事实库。

### 1.3 Session 保存什么

`app/stores/session.py` 的 InMemory/Redis 实现已经保存：

- Pending clarification，带版本号和 CAS；
- response idempotency 与 execution lease；
- `last_request`；
- 当前 `task_frame`；
- 最近 12 个去重 `task_frame`；
- 最近数据集引用；
- DAG checkpoint 与 DAG pending；
- report reference；
- Validated Query Recall 示例。

其中 `task_frame` 会移除 ASL，只保存规范化请求合同；这是正确的安全边界。但这些结构主要是短期运行缓存，不是完整 Conversation History。

### 1.4 Redis 保存什么

生产配置默认使用 Redis Session Store，默认 `session_ttl_seconds=7200`。Redis 中包含上述 Pending、Last Request、Task Frame、Recent Task Frames、Dataset Recent List、DAG 状态、幂等缓存及 Session Event List 等短期状态。近期 Task Frame 最多 12 个；Session Event 每个会话最多 500 个，并且跟随 Session TTL 过期。

结论：Redis 当前是**热状态与恢复缓存**，不应升级为唯一的长期历史事实源。

### 1.5 Long-term Memory 保存什么

`app/stores/long_memory.py` 与 `app/services/memory_manager.py` 已实现受治理的长期记忆：

- 类型包括用户偏好、指标别名、默认筛选、展示偏好、业务上下文；
- 强制 tenant/user/application 隔离；
- 候选、确认、替代、软删除和有效期；
- InMemory 与 MySQL 存储；
- 仅在用户明确说“记住/以后默认/设为默认”等情况下写入；
- 禁止保存 SQL、结果行、原始问题、聊天历史、Prompt、推理过程和凭据。

这是合理的 Semantic Memory，不应被改造成历史分析 Episode 仓库。

### 1.6 最近历史如何进入 Prompt

`ChatRequest.history` 由平台调用方传入，最多 100 条、总计 120,000 字符。`ContextBuilder` 在最多 32,000 字符预算内，优先选择已确认长期记忆、经过锚点保留的近期历史和工具摘要；默认最多 40 条历史，并进一步压缩。

`ContextCompactor` 在历史超过 24 条时生成确定性摘要并保留最近 12 条。摘要保存当前目标、指标、维度、筛选、修正、结论和未解决项，但它不是原始历史的持久存储。

### 1.7 Follow-up 如何判断

当前主要依据：

- `ConversationControl`：NEW_REQUEST、CLARIFICATION_RESPONSE、FOLLOW_UP、CORRECTION、CANCEL、FEEDBACK；
- 规则分类器识别“按医院看看、展示前五名、同比呢”等上下文信号；
- Pending 状态和缺失槽位；
- 当前 Task Frame / Last Request；
- `working_memory.py` 中“回到、恢复、之前、上次”等显式召回词；
- 新任务替换和独立闲聊的防污染规则。

这套机制对相邻轮次有效，但它将“追问”视为二元或少量控制状态，没有显式区分当前主题追问、历史主题返回和歧义主题引用。

### 1.8 Query Rewrite 是否存在

存在。`QuestionRewriter` 会：

- 基于一个 `previous: CanonicalAnalysisRequest` 补全上下文；
- 处理时间替换、确定性槽位更新、别名和部分实体规范化；
- 校验数字、否定词和关键语义是否被错误改变；
- 不安全时拒绝重写并降级。

问题在于它的输入通常只有一个“previous”，而不是从多个历史 Thread/Episode 检索后选出的目标状态。

### 1.9 Reference Resolution 是否存在

存在局部能力，但尚无统一 Reference Resolver：

- 实体别名与语义层规范化；
- Pending clarification 的槽位合并；
- 最近结果中仅有一个商品时，可解析“它卖给哪些医院”；
- 当前结果数据集上的 TopN、筛选、排序、选列等追问；
- 最近 Task Frame 的显式词法召回。

尚不能统一解析：历史主题指代、结果集指代、序数指代、对话时间指代和结果版本指代。

### 1.10 是否能搜索完整 Conversation History

不能。现有 `SessionEventStore.list_events()` 只能按受信作用域、会话和可选 trace_id 读取最近事件；没有全文、BM25、向量、实体、指标、Thread、Episode 或结果引用检索接口。调用方传入的 `history` 也只是本轮输入，不是本服务可自主搜索的长期历史库。

### 1.11 Context Compaction 后旧 Conversation 是否还能找回

不能保证。只要上游持续携带完整历史，旧消息可能仍在调用方；但 Data Agent 自身没有持久、可搜索的原始 Conversation Store。Redis TTL 到期、最近 Task Frame 被挤出 12 条或调用方不再发送旧消息后，就无法可靠召回。

### 1.12 为什么容易只理解上一轮

Orchestrator 首先选择 Pending、当前 Task Frame 或 Last Request；只有显式召回词才检查最近 12 个 Task Frame，而且只做词法匹配。因此解析天然偏向最新状态。Question Rewrite 和 merge 也围绕单个 previous 执行，没有候选检索、重排和历史状态快照恢复。

### 1.13 是否存在历史 Context 污染

存在风险，但已有不少防护。系统已经避免把独立闲聊和明显新任务强行合并到旧 Pending，也校验语义模型、数据库、业务域和知识库作用域。但细微 Topic Shift、同实体不同指标、同产品不同地区等场景仍可能继承错误槽位。根因是“选择哪个历史状态”与“如何合并槽位”尚未分层。

### 1.14 是否存在 Topic Shift 判断

有规则式的新任务替换与独立请求判断，但没有持久的 Thread 模型，也没有 `CURRENT_TOPIC_FOLLOWUP / HISTORICAL_TOPIC_RETURN / NEW_TOPIC / AMBIGUOUS_TOPIC_REFERENCE` 六类会话动作分类。

### 1.15 是否存在 Topic Return 能力

存在有限能力：显式出现“回到/恢复/之前/前面/上次”等词时，在最近 12 个结构化 Task Frame 中做词法召回；弱匹配或并列会拒绝。它不支持 20/50 轮、跨 TTL、语义召回、时间提示、Thread/Episode 恢复和结果版本恢复。

### 1.16 是否保存历史分析结果引用

保存了近期结果引用。数据库或文件结果可持久化为不可变 `DatasetReference`，包含数据集 ID、范围、列、行数、时间水位、来源、语义模型、业务域、指标、父数据集 ID 和 transformation log；大型内容存 MinIO，Redis 保存近期引用。

但当前只维护“最近列表”，缺少 Thread/Episode/Turn 绑定、root artifact、分支头、可搜索业务摘要和历史引用解析。

### 1.17 当前截图问题的精确执行路径

```text
A0：原始 SQL 结果，22 行
  └─ 用户：“只展示前五名”
      A1：LIMIT 5，parent=A0，5 行；A1 被 LPUSH 成最近引用
          └─ 用户：“展示前十名”
              自动选择 references[0] = A1
              LIMIT 10(A1) 仍为 5 行
          └─ 用户：“最开始的22条里面的前十条”
              当前没有“最开始/22条”结果版本解析
              仍选择 A1，结果仍为 5 行
```

因此这不是 SQL 计算错误，也不是 MinIO 覆盖了原结果。原始 A0 大概率仍存在，且 A1 已记录父引用；错误发生在**结果引用选择层**。

---

## 2. 当前问题根因

1. **Current State 被承担了 Entire Conversation State 的职责。** 一个 conversation key 只有一个主要活动 Task Frame；新主题会覆盖活动语义状态。
2. **历史存储与 Prompt 压缩混在了一起。** Context Summary 能省 Token，但不能替代可搜索的原始事件和 Episode。
3. **缺少 Thread 与 Episode。** 系统无法表达“这是同一个长期业务主题的第 4 次分析”，只能看到若干近期请求快照。
4. **召回窗口过短且仅词法。** 12 个 Task Frame、显式召回触发、简单词项得分，无法覆盖 20/50 轮和隐式业务指代。
5. **选择历史目标与重写当前问题没有分层。** Question Rewrite 在“选错 previous”之后再准确，也只会准确地继承错误上下文。
6. **缺少统一 Reference Resolution。** 实体、结果集、序数、历史主题、时间提示分别散落在规则、重写和 Dataset Follow-up 中。
7. **结果血缘只存不读。** `parent_dataset_ids` 和 transformation log 已记录，但选择器只从最近引用取第一个作用域匹配项。
8. **缺少可解释的候选与置信度。** 没有 top1/top2 margin、召回触发原因、候选 Thread/Episode 评分和歧义阈值。
9. **事件日志不是长期事件仓。** Redis 上限和 TTL 适合诊断，不适合长期搜索与状态重建。
10. **冷启动恢复过窄。** 当前 history recovery 只安全识别“历史用户问题 + 助手澄清”组合，不会从任意历史中恢复已完成分析。

核心判断：当前确实存在把 Current State 当作 Entire Conversation State 使用的结构性倾向。V2 应保留 Current State，但把它降级为 Active Thread 当前任务的临时运行态。

---

## 3. Letta 值得学习什么

参考：[Letta 当前代码仓库](https://github.com/letta-ai/letta-code)、[Letta 项目入口](https://github.com/letta-ai/letta)、[Memory Blocks](https://docs.letta.com/tutorials/attaching-detaching-blocks/)、[Archival Passages API](https://docs.letta.com/api/typescript/resources/agents/subresources/passages)。

值得借鉴的不是完整 Agent Runtime，而是职责分离：

- **当前上下文、持久记忆块、完整 transcript 分开。** 当前 Prompt 只放有限工作上下文；完整记录独立持久化。
- **Context Window 之外仍可搜索。** 当前 Letta Code 将本地 transcript 以 JSONL 保存，并可按 agent、conversation、时间和内容搜索；搜索结果再被带回上下文。
- **Compaction 不等于删除。** 压缩摘要保留目标、历史进展、文件/ID、错误、当前状态和检索线索，而原始 transcript 仍独立存在。
- **可编辑的稳定记忆块。** Persona、Human 等长期块是结构化、可维护的信息，不是聊天原文堆积。

回答“为什么几十轮以前还能找回来”：因为历史没有只存在于当前 messages；完整 transcript/archival data 被独立保存并提供搜索工具，模型可在需要时主动或由运行时触发检索。它避免最近消息依赖的关键，是**外部持久化 + 可检索索引 + 有界上下文装配**。

不建议照搬：Letta 的完整 Runtime、消息格式、Agent Loop 或其所有内存抽象。当前项目已有 LangGraph、Orchestrator、Session、Tool 和 Skill，应该只吸收“持久历史与当前上下文分层”的原则。

---

## 4. LangMem 值得学习什么

参考：[LangMem 官方文档](https://langchain-ai.github.io/langmem/)、[LangMem 概念指南](https://langchain-ai.github.io/langmem/concepts/conceptual_guide/)、[LangMem 仓库](https://github.com/langchain-ai/langmem)。

LangMem 对本项目最重要的启示是内存类型和写入时机必须分开：

| 类型 | 含义 | 本项目示例 | 推荐存储 |
|---|---|---|---|
| Semantic Memory | 稳定事实、偏好、业务知识 | 用户默认关注销售额、默认按月、常用上海区域 | 复用现有 LongTermMemory |
| Episodic Memory | 过去完成过的经历/任务 | 曾分析上海紫杉醇球囊趋势，11 月出现下降 | 新增 AnalysisEpisode |
| Procedural Memory | Agent 如何工作 | SQL 安全规则、分析 Skill 说明、输出策略 | 现有代码、Prompt、Skill 配置 |

还应借鉴：

- 热路径只读取本轮必要信息，避免每轮做昂贵全局检索；
- 后台从已完成事件中形成 Thread/Episode/可搜索摘要；
- Profile 适合稳定、唯一的用户设置；Collection 适合多条 Episode；
- LangGraph Store 的 namespace/作用域思想适合 tenant/user/application/conversation 隔离。

不应把“以前分析过什么”写进现有用户偏好表，也不应将稳定偏好和历史结果全部塞进同一个向量库。

---

## 5. Graphiti 值得学习什么

参考：[Graphiti 仓库](https://github.com/getzep/graphiti)、[Graphiti 概览](https://help.getzep.com/graphiti/getting-started/welcome)、[Adding Episodes](https://help.getzep.com/graphiti/core-concepts/adding-episodes)、[Search](https://help.getzep.com/graphiti/working-with-data/searching)。

适合当前项目的设计思想是 `Episode + Entity + Time + Provenance`：

- Episode 保存一次原始输入及其来源；
- Entity 将产品、地区、医院、经销商、指标等业务对象规范化；
- 关系保存“由哪个 Episode 得出”的 provenance；
- `valid_at/invalid_at` 表示业务事实何时有效，`created_at/expired_at` 表示系统何时知道或撤销它，形成双时间语义；
- 检索可组合语义、关键词、图距离、Episode 提及、MMR 和 Cross Encoder 重排。

适用方式：第一阶段用关系表表达 Episode—Entity、Thread—Episode、Result—Parent 即可；只有当跨会话、多实体、多跳关系查询成为明确瓶颈时，再评估图数据库。当前不应直接引入 Neo4j/FalkorDB，也不应把全部 SQL 元数据复制成第二套知识图谱。

---

## 6. Open Data QnA 值得学习什么

参考：[GoogleCloudPlatform/Open_Data_QnA](https://github.com/GoogleCloudPlatform/Open_Data_QnA)、[Google Data QnA 背景介绍](https://cloud.google.com/blog/products/data-analytics/introducing-data-qna)。

仓库实现中的有价值部分：

- 将历史 question/SQL 记录在 session history；
- 在 SQL 生成前，把当前追问结合历史重写为 self-contained question；
- 使用已验证的问法/SQL 示例、Schema 和列信息做检索增强；
- SQL 生成后进行 validation/debug，并通过 dry run/full run 执行；
- 将自然语言解释、SQL、执行结果和可视化串成数据问答链。

对当前项目的启发是：历史解析必须发生在 Text-to-SQL 之前，最终输入语义层的应是独立可理解的 Canonical Query；Validated Query 只能作为语义范例，不能直接复制历史 SQL。

其局限也要明确：直接拼接完整 session history 和偏重 last question/last SQL，仍然更适合短距离追问，不足以替代 Thread/Episode/Result Reference 架构。

---

## 7. AmbiSQL 值得学习什么

参考：[AmbiSQL 仓库](https://github.com/JustinzjDing/AmbiSQL)、[AmbiSQL 论文](https://arxiv.org/abs/2508.15276)。

可借鉴的流程：

```text
Ambiguity Detection
  -> 识别歧义类型和候选解释
  -> 生成有边界的澄清问题/选项
  -> 保存用户选择证据
  -> 在不丢失原约束的前提下重写问题
  -> 再进入 Text-to-SQL
```

其歧义分类覆盖 Schema、Value、View、Source、Context、Fallacy 和 Reference。对本项目最有价值的是 AmbiRef/AmbiContext：

- “之前上海那个球囊”可能指销售额 Episode，也可能指销售量 Episode；
- “第二个”必须先找到唯一结果集，再验证结果顺序和行数；
- 新信息只覆盖明确冲突的槽位，其余原约束继续保留。

不建议每轮都运行昂贵歧义模型。仅在 Local Resolution 低置信度、Global Retrieval top1/top2 接近或序数引用缺少唯一结果集时触发。

---

## 8. 当前已有能力

以下能力应复用，不应重复开发：

1. 受信 tenant/user/application/conversation 作用域与请求校验。
2. CanonicalAnalysisRequest、SemanticContextSnapshot、RelationshipPathRef 等结构化语义合同。
3. Pending clarification、版本号、CAS、防并发覆盖。
4. Last Request、Current Task Frame、最近 12 个 Task Frame。
5. Question Rewrite 的确定性时间/槽位更新和安全回退。
6. 新任务替换、独立闲聊、防历史筛选污染的规则。
7. 多任务 DAG、依赖约束、Redis checkpoint 和恢复。
8. Session Event 的统一事件类型、载荷脱敏和 Trace Summary。
9. 受治理 LongTermMemory：确认、替代、软删除、有效期、MySQL。
10. 不可变 MinIO Dataset、作用域校验、TTL、校验和。
11. `parent_dataset_ids` 与 transformation log 的结果血缘。
12. Dataset Follow-up 的白名单操作：筛选、排序、TopN、聚合、选列、派生列。
13. SQL/ASL/结果质量的多层验证和只读安全。
14. 确定性趋势、对比、构成、异常、归因等分析 Skill。
15. Evidence、Reliability、Structured Result、报告和图表输出。
16. Validated Query Recall 的作用域隔离、仅保存通过校验的语义范例、禁止保存 SQL/ASL/rows。
17. ContextBuilder 的字符预算、锚点历史和工具摘要。
18. SSE 事件协议及当前前端展示合同。

V2 的重点不是再造 Memory、Skill、Result Store 或 Checkpoint，而是把这些已有能力通过 Thread/Episode/Reference 连接起来。

---

## 9. 真正缺失能力

| 缺失能力 | 当前影响 | V2 目标 |
|---|---|---|
| Durable Conversation Event Store | Redis TTL 后历史不可检索 | 原始事件长期可查，摘要不替代原文 |
| Analysis Thread | 新旧主题无稳定业务身份 | 同一业务主题跨多轮聚合 |
| Analysis Episode | 无重要分析快照 | 可召回完成过的分析及其状态 |
| Semantic State Snapshot | 只能恢复 Thread 最新状态 | 可恢复任意重要 Turn/Episode |
| Topic Action Classifier | FOLLOW_UP 粒度过粗 | 六类会话动作 |
| Historical Retrieval | 仅最近 12 帧词法匹配 | Local/Global 两级混合召回 |
| Rerank 与阈值 | 无 top1/top2 置信边界 | 可解释选中、歧义时澄清 |
| Unified Reference Resolver | 规则分散 | 实体/结果/序数/时间/主题统一解析 |
| State Restore + Delta | previous 整体继承 | 历史快照 + 当前明确增量 |
| Result Artifact Version Selection | 只选最新数据集 | root/parent/branch/ordinal 解析 |
| Long Conversation Eval | 短用例通过但远距离失败 | 20/50 轮专门评测 |
| Recall Observability | 无候选评分链 | Langfuse 可定位错层 |

---

## 10. 不建议增加的能力

1. 不引入完整 Letta/MemGPT Runtime。
2. 不引入完整 LangMem 作为第二套 Memory Runtime；可借鉴其分类，继续使用本项目存储接口。
3. 第一阶段不引入 Neo4j、FalkorDB 或完整 Graphiti。
4. 不把全部聊天、Episode、偏好、结果行写入一个 Vector Store。
5. 不扩大 `messages[-N:]` 或把全部历史直接塞进 Prompt。
6. 不用单一 LLM Prompt 同时完成召回、选择、状态合并和 SQL 生成。
7. 不增加 ConversationManager/ThreadManager/EpisodeManager/MemoryManager/ContextManager 多层空包装。
8. 不复制历史 SQL；Validated Query 只提供语义结构范例。
9. 不把大结果行放进 LangGraph State、MySQL Event payload 或 LLM Context。
10. 不修改现有 SSE 标签、前端消息结构和稳定业务输出合同来解决后端记忆问题。

---

## 11. Conversation Event Store 设计

目标：提供不可变、长期、可检索的对话事实源。Conversation Summary 只是派生视图。

### 11.1 事件原则

- append-only；修正通过新事件表达，不原地覆盖；
- 强制 tenant/user/application/conversation ACL；
- 原始问题与规范化问题分字段保存；
- 大结果只保存 Artifact ID、Schema、行数和摘要；
- 支持按 conversation、turn、thread、episode、entity、metric、时间和关键词检索；
- event_id 幂等，turn_id 单调递增；
- 保留数据水位和算法版本，保证未来可解释。

### 11.2 推荐事件

`TURN_RECEIVED`、`TURN_CLASSIFIED`、`REFERENCE_RESOLVED`、`THREAD_SELECTED`、`STATE_RESTORED`、`QUERY_CANONICALIZED`、`TOOL_COMPLETED`、`RESULT_ARTIFACT_CREATED`、`ANALYSIS_COMPLETED`、`ANSWER_COMPLETED`、`CLARIFICATION_REQUESTED`、`USER_CORRECTION`、`THREAD_CLOSED`。

现有 `SessionEventType` 可以增量扩展，运行诊断事件仍写 Redis；业务级关键事件异步或事务后写入持久 Event Store。推荐第一阶段使用现有 MySQL 基础设施；Redis 只保留热索引和最近候选。

---

## 12. Analysis Thread 设计

Thread 表示一个可持续返回的业务分析主题，不等于一次 SQL，也不等于整个会话。

推荐字段：

```json
{
  "thread_id": "thr_...",
  "conversation_id": "conv_...",
  "topic_label": "上海紫杉醇释放冠脉球囊导管",
  "created_turn_id": 1,
  "last_active_turn_id": 10,
  "status": "ACTIVE|SUSPENDED|CLOSED",
  "entity_refs": [{"type": "region", "id": "上海市"}, {"type": "product", "id": "..."}],
  "metric_refs": ["annual_total_sales"],
  "analysis_types": ["TREND_ANALYSIS", "ROOT_CAUSE_ANALYSIS"],
  "current_state_snapshot_id": "snap_...",
  "semantic_summary": "上海地区该产品的销售趋势与下降原因分析",
  "important_findings": ["11月下降，12月部分修复"],
  "episode_ids": ["ep_1", "ep_2"],
  "result_artifact_ids": ["ds_a0", "ds_a2"],
  "version": 4
}
```

Thread 创建/切换规则：

- 明确“换个问题/新问题”或主题实体集合显著改变：创建新 Thread；
- 仅改变地区、维度、指标或时间且用户表达延续：在同 Thread 新建 Episode；
- 明确“回到最开始上海那个”：检索并重新激活历史 Thread；
- 候选接近时不切换，先 Clarification；
- 切换 Thread 不删除原 Thread，只更新 active pointer。

---

## 13. Analysis Episode 设计

Episode 是一次已经完成或进入明确澄清状态的重要分析单元。它不是聊天原文副本，而是为未来召回准备的结构化经历。

```json
{
  "episode_id": "ep_...",
  "thread_id": "thr_...",
  "source_turn_ids": [1],
  "status": "COMPLETED",
  "user_goal": "分析上海该球囊整体销售趋势",
  "canonical_query": "分析上海市紫杉醇释放冠脉球囊导管含税销售总额月度趋势",
  "semantic_state_snapshot_id": "snap_...",
  "analysis_type": "TREND_ANALYSIS",
  "selected_skills": ["metric_query", "trend_analysis"],
  "entity_refs": ["region:上海市", "product:紫杉醇释放冠脉球囊导管"],
  "metric_refs": ["81:annual_total_sales"],
  "result_artifact_ids": ["ds_a0"],
  "findings": [{"claim": "11月下降", "evidence_ids": ["ev_1"]}],
  "answer_summary": "整体下降，11月触底，12月部分修复",
  "data_as_of": "2025-12-30T00:00:00+00:00",
  "created_at": "..."
}
```

形成时机：成功完成的查询/分析、用户确认后的澄清任务、产生可复用 Result Artifact 的重要任务。普通寒暄、重复刷新、失败且无可靠状态的请求不形成 Episode。

---

## 14. State Snapshot 设计

State Snapshot 保存某个 Episode 当时**已确认的语义状态**，而不是所有运行中间变量。

建议包含：

- semantic model、database、business domains、knowledge scopes；
- canonical entities、metrics、filters、dimensions、fields；
- time range、comparison type、analysis type、operators、ranking limit；
- relationship paths 与语义资产版本；
- result artifact IDs；
- assumptions、ambiguities、data watermark；
- source turn、thread、episode、schema version。

明确不包含：凭据、原始结果行、完整 Prompt、链式思考、临时执行句柄。

每个重要 Episode 形成不可变 Snapshot；Thread 另存一个 `current_state_snapshot_id` 指向最新有效状态，但历史 Snapshot 不覆盖。

---

## 15. Historical Retrieval 设计

新增三个清晰的读取能力，而不是一个万能 Memory Search：

```python
conversation_search(scope, conversation_id, query, filters, limit)
search_analysis_threads(scope, conversation_id, retrieval_query, limit)
search_analysis_episodes(scope, conversation_id, retrieval_query, limit)
```

检索输入 `RetrievalQuery` 应包含：

- 当前原始问题和去指代后的关键词候选；
- conversation temporal hint：FIRST、EARLIEST、PREVIOUS、RECENT、BEFORE_TURN、AROUND_TIME；
- entity/metric/analysis type/filter hints；
- expected reference type：THREAD、EPISODE、RESULT、ENTITY；
- active thread ID 与排除项；
- ACL scope。

返回值不能只有文本，应返回候选 ID、结构化摘要、匹配证据、各子分数、来源 Turn 和可恢复 Snapshot ID。

---

## 16. Hybrid Retrieval 设计

### 16.1 两级召回

```text
Level 1 — Local Context Resolution
  Active Thread + Current State + 最近明确 Turn + 当前结果分支
  高置信度 -> 直接进入 Reference Resolution

Level 2 — Global Historical Recall
  触发条件：显式历史词、Local 低置信度、多个候选、结果版本提示
  检索 Conversation Events + Threads + Episodes + Result Artifacts
```

### 16.2 Hybrid 信号

1. ACL/scope：硬过滤，任何不一致直接淘汰。
2. Semantic model/business domain compatibility：硬过滤或强约束。
3. Entity match：规范化 ID 精确匹配优先于名称模糊匹配。
4. Metric/analysis type match。
5. Keyword/BM25：产品长名称、医院名、地区名特别有效。
6. Semantic embedding：处理“球囊/那个介入耗材”等语义变体。
7. Conversation temporal hint：“最开始”偏向 created_turn 最小的匹配候选。
8. Recency：仅在没有显式历史方向时作为弱加分。
9. Result reference compatibility：序数/前 N 必须候选 Episode 有有序结果集。
10. Thread continuity：Active Thread 是局部先验，但不能压过“最开始”等强信号。

第一阶段可以只用 MySQL metadata + 倒排关键词 + 现有实体规范化完成可靠基线；Embedding 为可选增强，不应成为唯一召回通道。

---

## 17. Rerank 设计

推荐先采用可审计的确定性加权，再在有评测集后校准：

```text
score =
  0.28 * entity_match
+ 0.18 * metric_analysis_match
+ 0.16 * keyword_bm25
+ 0.12 * temporal_hint_match
+ 0.10 * result_reference_compatibility
+ 0.08 * semantic_similarity
+ 0.05 * thread_continuity
+ 0.03 * recency
- risk_penalty
```

规则：

- 作用域不匹配直接拒绝；
- 强时间提示覆盖普通 recency；
- 需要“第二个”但候选没有稳定排序结果时增加高风险惩罚；
- `top1 < accept_threshold`：澄清或视为新主题；
- `top1 - top2 < ambiguity_margin`：澄清；
- 自动恢复阈值应高于“提供候选供用户选”的阈值；
- 所有分数、阈值、版本和命中信号写 Trace。

初始阈值不可凭感觉上线，必须通过第 26 节评测集标定。目标优先级是降低 False Recall，而不是最大化自动召回率。

---

## 18. Reference Resolution 设计

统一输出 `ReferenceResolution`：

```json
{
  "reference_type": "HISTORICAL_TOPIC|RESULT_SET|ORDINAL|ENTITY|TEMPORAL",
  "surface": "最开始上海那个球囊",
  "selected_thread_id": "thr_a",
  "selected_episode_id": "ep_a1",
  "selected_result_artifact_id": "ds_a0",
  "resolved_entities": ["region:上海市", "product:..."],
  "ordinal": null,
  "confidence": 0.94,
  "evidence": ["EARLIEST_HINT", "EXACT_REGION", "PRODUCT_ALIAS"],
  "alternatives": [],
  "requires_clarification": false
}
```

处理顺序：

1. 识别引用表达和期望类型；
2. Local Resolution；
3. 必要时 Global Retrieval；
4. Rerank；
5. 校验目标是否支持当前操作；
6. 高置信度绑定；低置信度产生结构化 Clarification；
7. 将解析结果交给 State Restore，而不是直接改写 SQL。

序数解析必须满足：唯一 Result Artifact、稳定排序、索引在范围内、结果版本未过期。否则不得猜测“第二个”。

---

## 19. Thread Switch 设计

会话动作扩展为：

| 动作 | 示例 | 行为 |
|---|---|---|
| CURRENT_TOPIC_FOLLOWUP | 按医院看看 | 使用 Active Thread 当前 Snapshot |
| HISTORICAL_TOPIC_RETURN | 还是最开始上海那个 | 检索历史 Thread/Episode 并恢复 |
| NEW_TOPIC | 换个问题，分析阿司匹林 | 创建 Thread，不继承旧业务槽位 |
| AMBIGUOUS_TOPIC_REFERENCE | 之前上海那个球囊 | 给出候选澄清 |
| CORRECTION | 不是上海，是北京 | 在目标 Thread 新建修正 Episode |
| CLARIFICATION_RESPONSE | 第一个 | 恢复 Pending Resolution 再继续 |

分类采用规则优先、模型补充：显式“换个问题/回到/最开始/之前/第二个”走确定性特征；实体/指标大幅变化用结构化差异；仅在边界场景调用模型。分类器只判断动作，不同时承担历史候选选择。

---

## 20. State Restore 设计

正确流程：

```text
Selected Historical Episode
  -> load immutable Semantic State Snapshot
  -> verify current ACL / semantic model / asset versions
  -> extract Current Turn Delta
  -> apply explicit replacement rules
  -> produce Merged Resolved State
  -> run semantic validation
  -> create a new Episode/Snapshot after completion
```

合并原则：

- 当前明确值覆盖历史值；
- 未提及的稳定槽位继承；
- “最开始上海那个”优先恢复最初 Episode，而不是 Thread 最新的北京状态；
- 时间范围默认不盲目继承，需结合追问类型和当前数据水位；
- semantic model/domain/ACL 不兼容时拒绝恢复；
- 指标、实体或时间有多个合理解释时澄清；
- 历史 Snapshot 永不原地修改。

示例：历史 A1 = 上海 + 紫杉醇球囊 + 销售额；当前“换成销售量”形成 Delta(metric=销售量)，最终状态为上海 + 同产品 + 销售量。

---

## 21. Result Reference 设计

### 21.1 在现有 DatasetReference 上增强元数据

保留现有不可变 Dataset 和 MinIO 存储，增加可检索元数据：

- `thread_id`、`episode_id`、`turn_id`；
- `root_dataset_id`；
- `parent_dataset_ids`；
- `branch_id` 与 `operation_index`；
- `result_role`：QUERY_ROOT、DERIVED_VIEW、JOINED、UPLOADED；
- `stable_sort_spec`；
- `business_summary`；
- `entity_refs`、`metric_refs`；
- `supersedes` 仅表示活动视图，不删除旧版本。

### 21.2 版本选择规则

- “刚才结果前 5 条”：使用 Active Branch Head；
- “展示前 10 条”，若当前 head 是 LIMIT 5 且其祖先有至少 10 行：自动选择最近满足条件的祖先；
- “最开始 22 条里的前 10 条”：强制解析 `EARLIEST/ROOT`，选择 A0，再生成 A2；
- “撤销刚才筛选”：选择 parent；
- “第二个”：选择引用 Episode 的稳定有序结果，而不是最新无关结果；
- 若 root 已过期或没有稳定排序，明确提示重新执行或澄清。

截图修复后的血缘应为：

```text
A0 (QUERY_ROOT, 22 rows)
├── A1 (LIMIT 5, parent=A0, root=A0)
└── A2 (LIMIT 10, parent=A0, root=A0)  <- “最开始22条里的前十条”
```

这是一项高优先级、低侵入改造，因为现有血缘字段和不可变存储已经具备。

---

## 22. Context Builder 设计

最终模型上下文应改为结构化 Envelope：

```text
ImmediateContext
  当前原始问题、受信作用域、当前 turn

ActiveThreadContext
  thread 摘要、当前 snapshot、未解决槽位、当前 result head

RetrievedHistoricalContext
  最多 1~3 个候选的结构化摘要、匹配原因、turn/episode/result IDs

SemanticMemory
  用户已确认偏好、业务知识、别名

RelevantResultReference
  schema、row_count、排序、root/parent、少量安全 preview
```

规则：

- 不把检索到的 10 轮原文全部加入 Prompt；
- 历史原文仅在需要核验引用时按 event_id 取小片段；
- 每个字段带 source/provenance；
- 当前明确输入与历史恢复字段分开，便于模型知道哪些可覆盖；
- 大结果只提供引用和摘要；
- 保留现有字符预算与脱敏逻辑；
- Question Rewriter 读取 `ResolvedConversationContext`，不再只读取单个 previous。

---

## 23. Memory 职责划分

| 组件 | 保存内容 | 生命周期 | 当前/新增 |
|---|---|---|---|
| Conversation Event Store | 原始与结构化对话事件 | 长期、可搜索 | 增强现有 Events |
| Analysis Thread | 连续业务主题聚合 | 会话级/长期 | 新增投影视图 |
| Analysis Episode | 完成过的重要分析 | 长期、不可变 | 新增 |
| Semantic Memory | 稳定偏好、别名、业务上下文 | 用户级、受治理 | 复用 LongTermMemory |
| Result Artifact | 大型查询/分析结果及血缘 | TTL/归档策略 | 增强 DatasetReference |
| Current State | 当前 Active Thread 运行态 | 单次运行/热状态 | 复用 AgentState/Session |
| Conversation Summary | Prompt 压缩视图 | 可重建 | 复用并增加来源指针 |
| Validated Query Recall | 通过校验的语义范例 | 模型/业务域级 | 复用，禁止 SQL 复制 |

禁止将上述对象合并成一个泛化 Memory Vector Store。

---

## 24. LangGraph 融合方案

继续使用现有 LangGraph，不建立第二套 Runtime。推荐在现有流程前部加入职责明确的节点：

```text
request_validation
  -> conversation_event_append
  -> turn_classification
  -> local_context_resolution
  -> conditional historical_retrieval
  -> reference_resolution
  -> conditional clarification interrupt
  -> state_restore_and_delta_merge
  -> existing question/intent/semantic/tool/analysis pipeline
  -> episode_materialization
  -> final_output
```

融合策略：

1. 第一阶段先在 Orchestrator 前以 shadow mode 计算候选，不改变结果。
2. 当前自建 Pending CAS 和 DAG checkpoint 继续使用，避免一次性迁移。
3. LangGraph `interrupt/resume` 可在后续用于历史候选澄清，但必须与现有 PendingState 建立单一权威状态；不能两个系统各存一份待补充状态。
4. 若启用 LangGraph checkpointer，只存有界 State 和引用，不存结果行。
5. Episode materialization 由完成事件投影，不阻塞主回答；但 Result Reference 和 Snapshot 的关键 ID 应在最终事件前可靠写入。
6. 图节点输出必须继续遵守现有 SSE stage/type 合同。

---

## 25. Langfuse Trace 方案

现有 `TraceSummary` 主要覆盖请求、工具、分析、结果校验。V2 增加以下 span/attributes：

```text
turn_classification
  action, confidence, rules_hit

reference_resolution
  reference_types, surfaces, confidence, requires_clarification

historical_recall_trigger
  triggered, reason, local_confidence

retrieval_query
  temporal_hint, entity_ids, metric_ids, expected_reference_type

candidate_threads / candidate_episodes
  ids, component_scores, rank, ACL_filtered_count

retrieval_rerank
  top1, top2, margin, thresholds, ranker_version

selected_thread / selected_episode
  selected_ids, provenance_turn_ids

state_snapshot / restored_state / current_delta / merged_state
  field-level hashes or bounded summaries, never sensitive rows

result_reference
  dataset_id, root_id, parent_ids, selected_reason, row_count, operation

skill_transition
  previous_skill, selected_skill, reason
```

每次“最开始那个”判断错误时，应能回答：是否触发 Global Recall、候选有哪些、为什么某候选得分最高、是召回错、重排错、引用解析错，还是状态合并错。

---

## 26. Evaluation 方案

### 26.1 必测案例

**Case A：20 轮远距离明确返回**  
Turn 1 为上海球囊趋势；Turn 2~19 无关；Turn 20 问“还是最开始上海那个，为什么 11 月下降？”必须命中 Turn 1 Thread/Episode。

**Case B：同实体不同指标歧义**  
Turn 1 销售额、Turn 8 销售量、Turn 20 问“之前上海那个球囊”。必须澄清，不能随机继承。

**Case C：历史结果 + 序数**  
Turn 3 上海 Top3 医院；Turn 30 问“之前上海那三个医院里第二个今年怎么样？”必须恢复历史 Result Artifact、稳定顺序和第二行实体。

**Case D：50 轮超长返回**  
Turn 1 主题 A，Turn 2~49 无关，Turn 50“继续最开始那个”。验证持久检索，不依赖最近消息。

**Case E：截图结果分支**  
22 条 A0 → 前 5 条 A1 → 前 10 条 → 最开始 22 条中的前 10 条。期望最后选择 A0 并产生 10 行 A2。

**Case F：作用域隔离**  
相同名字存在于不同 tenant/application/semantic model，任何召回不得越界。

**Case G：历史结果过期**  
Episode 仍可召回，但 Result Artifact 已过期。系统应解释需重新查询，不得用其他近期结果替代。

### 26.2 指标

| 指标 | 定义 |
|---|---|
| Historical Topic Recall@1/@3 | 正确 Thread 是否排在前 1/3 |
| Episode Retrieval Recall | 正确 Episode 是否被召回 |
| Reference Resolution Accuracy | 引用类型和目标 ID 同时正确 |
| Thread Switch Accuracy | 六类会话动作分类正确率 |
| State Restore Accuracy | 恢复后各槽位与金标一致 |
| Long-range Follow-up Accuracy | 20/50 轮后最终回答任务正确率 |
| Result Reference Accuracy | root/parent/ordinal/result 选择正确率 |
| Clarification Accuracy | 应追问时追问、不应追问时不追问 |
| False Recall Rate | 错误自动绑定历史主题的比例，核心红线 |
| Retrieval p50/p95 Latency | Local 与 Global 分别统计 |

上线门槛建议优先约束 False Recall Rate 和作用域泄漏为零，再优化自动召回率与延迟。

---

## 27. 数据结构设计

建议使用少量核心结构，避免 Manager 泛滥。

```python
class ConversationEvent:
    event_id: str
    conversation_id: str
    turn_id: int
    message_id: str
    tenant_id: str
    user_id: str
    application_id: str
    occurred_at: datetime
    event_type: str
    raw_query: str | None
    canonical_query: str | None
    thread_id: str | None
    episode_id: str | None
    entity_refs: list[EntityRef]
    metric_refs: list[str]
    analysis_type: str | None
    result_artifact_ids: list[str]
    payload_summary: dict
    schema_version: str

class AnalysisThread:
    thread_id: str
    scope: ConversationScope
    topic_label: str
    status: str
    created_turn_id: int
    last_active_turn_id: int
    entity_refs: list[EntityRef]
    metric_refs: list[str]
    analysis_types: list[str]
    current_snapshot_id: str | None
    episode_ids: list[str]
    result_artifact_ids: list[str]
    semantic_summary: str
    version: int

class AnalysisEpisode:
    episode_id: str
    thread_id: str
    source_turn_ids: list[int]
    user_goal: str
    canonical_query: str
    snapshot_id: str
    selected_skills: list[str]
    result_artifact_ids: list[str]
    findings: list[EvidenceBackedFinding]
    answer_summary: str
    data_as_of: datetime | None

class SemanticStateSnapshot:
    snapshot_id: str
    thread_id: str
    episode_id: str
    semantic_scope: SemanticScope
    entities: list[EntityRef]
    metrics: list[MetricRef]
    filters: list[FilterSpec]
    dimensions: list[str]
    time_range: TimeRange | None
    analysis_type: str
    operators: list[str]
    result_artifact_ids: list[str]
    assumptions: list[str]
    source_turn_id: int
    schema_version: str

class HistoricalCandidate:
    target_type: Literal["THREAD", "EPISODE", "RESULT"]
    target_id: str
    summary: str
    source_turn_ids: list[int]
    component_scores: dict[str, float]
    total_score: float
    match_evidence: list[str]

class ReferenceResolution:
    reference_type: str
    surface: str
    selected_thread_id: str | None
    selected_episode_id: str | None
    selected_result_artifact_id: str | None
    ordinal: int | None
    confidence: float
    alternatives: list[HistoricalCandidate]
    requires_clarification: bool

class ResultArtifactMetadata:
    dataset_id: str
    thread_id: str
    episode_id: str
    turn_id: int
    root_dataset_id: str
    parent_dataset_ids: list[str]
    branch_id: str
    result_role: str
    row_count: int
    columns: list[str]
    stable_sort_spec: list[dict]
    transformation_log: list[dict]
    entity_refs: list[EntityRef]
    metric_refs: list[str]
    expires_at: datetime
```

数据库建议：MySQL 保存 Event/Thread/Episode/Snapshot/Artifact Metadata；MinIO 继续保存结果内容；Redis 保存 active_thread、热候选、Pending、DAG 和短期缓存。若未来引入 Embedding，可为 Thread/Episode 摘要增加向量索引，不改变主数据模型。

---

## 28. 模块修改清单

按职责归并后的最小模块边界：

1. **Conversation history store**：持久写事件，按结构字段与文本检索。
2. **Thread/Episode projector**：从已完成事件形成或更新 Thread，并创建不可变 Episode/Snapshot。
3. **Historical retrieval**：生成检索条件、执行 Local/Global Hybrid Retrieval、返回候选。
4. **Reference resolution**：解析主题、结果、序数、实体和时间引用，输出置信度/澄清。
5. **State restore**：加载 Snapshot，应用 Current Delta，生成 Canonical Request。
6. **Result artifact resolver**：沿 root/parent/branch 选择正确数据集。
7. **ContextBuilder 增强**：消费结构化 Active/Historical/Result Context。
8. **Tracing/Evaluation 增强**：记录召回链并提供长会话指标。

其中 2 和 5 可以是函数/服务，不需要分别创建 Manager；Result artifact resolver 可先放入现有 dataset follow-up 模块。

---

## 29. 哪些现有文件需要改

实施阶段预计修改以下现有文件；本报告阶段未做任何修改：

| 文件 | 增量修改 |
|---|---|
| `app/domain/models.py` | 扩展会话动作、引用解析和历史候选合同 |
| `app/graph/state.py` | 增加 active_thread、retrieval、resolution、restored snapshot 引用 |
| `app/graph/workflow.py` | 加入分类、召回、解析、恢复节点；后期评估 checkpointer/interrupt |
| `app/services/orchestrator.py` | 在现有 rewrite/classify 之前接入解析结果；去除“直接拿一个 previous”的隐式选择 |
| `app/services/question_rewriter.py` | 输入改为已解析的结构化上下文，不负责挑选历史主题 |
| `app/services/working_memory.py` | 保留为 Local Recall，输出候选和分数，不冒充全局记忆 |
| `app/services/context_builder.py` | 新增 ActiveThread/RetrievedHistory/ResultReference 分区 |
| `app/services/context_compaction.py` | 摘要保存来源 event/episode 指针，明确可重建 |
| `app/stores/events.py` | 事件合同扩展；Redis 仍作热日志，接入持久实现 |
| `app/stores/session.py` | 增加 active_thread 指针、热候选；不承担长期主存储 |
| `app/services/dataset_followup.py` | 解析前 N、序数、撤销、root/ancestor 需求 |
| `minio_followup_store.py` | 增强 Result Artifact 元数据，但保持对象不可变与兼容旧引用 |
| `app/observability/tracing.py` | 增加召回、重排、状态恢复和结果版本 span |
| `app/dependencies.py` | 注入持久 history store 和 retrieval/resolver |
| `app/config.py` | 增加受控阈值、Global Recall 开关、shadow mode 配置 |
| `app/api.py` | 仅在需要时传递新 trace 元数据；不改变现有 SSE 标签和输出结构 |

明确不建议重写 `long_memory.py`、语义查询适配器、SQL 安全链、分析 Skill、Validated Query Recall 和现有输出协议。

---

## 30. 哪些文件真正需要新增

建议只新增四个生产模块和相应迁移/测试：

```text
app/conversation/models.py
    Thread、Episode、Snapshot、HistoricalCandidate、ReferenceResolution

app/stores/conversation_history.py
    ConversationEvent/Thread/Episode/Snapshot 的持久接口与 MySQL 实现

app/services/historical_retrieval.py
    Local/Global 触发、Hybrid Retrieval、Rerank

app/services/reference_resolution.py
    Topic/Result/Ordinal/Temporal/Entity 统一解析与 State Restore 辅助

migrations/<version>_conversation_history_v2.sql

tests/test_long_range_conversation.py
tests/test_historical_retrieval.py
tests/test_reference_resolution.py
tests/test_result_artifact_lineage.py
```

若 `conversation_history.py` 后续过大，再按数据访问与投影拆分；第一阶段不要预先创建一组 Manager。

---

## 31. 实施 Phase 划分

为避免与项目已有 Phase 编号冲突，建议命名为 V2-A 至 V2-G。

### V2-A：合同与基线

- 固化六类会话动作、Thread/Episode/Snapshot/Reference 合同；
- 建立 20/50 轮金标用例和当前基线；
- 不改变线上路由。

验收：数据模型评审通过，旧测试全绿，五个核心案例有可复现失败基线。

### V2-B：Durable Event + Thread/Episode Shadow Projection

- MySQL 持久 Conversation Event；
- 完成后异步投影 Thread/Episode/Snapshot；
- Shadow mode 只记录，不参与回答。

验收：原始事件可搜索，Summary 删除后可由事件重建，作用域隔离测试通过。

### V2-C：Result Artifact Lineage Resolver

- 增加 root/branch/stable sort 元数据；
- 实现 parent/root/ancestor 选择；
- 先修复 22 → 5 → 10 截图用例。

验收：Case E 通过，旧 Dataset Follow-up 行为兼容。

### V2-D：Local/Global Hybrid Retrieval Shadow Mode

- Local 先行、按触发词/低置信度进入 Global；
- 记录候选、分数、margin，不影响线上选择；
- 用评测集校准阈值。

验收：Recall@1/@3、延迟和 False Recall 达到门槛。

### V2-E：Reference Resolution + Thread Switch + Clarification

- 接入六类动作；
- 支持历史 Topic/Result/Ordinal 引用；
- 低置信度走现有 Pending clarification。

验收：Case A/B/C/D/F/G 通过，False Recall 低于设定红线。

### V2-F：State Restore + ContextBuilder + LangGraph

- Historical Snapshot + Current Delta 合并；
- ContextBuilder 改为五分区 Envelope；
- 将相关能力显式放入 LangGraph 节点；
- 评估以现有 Pending 为权威的 interrupt/resume 接入。

验收：State Restore Accuracy、Long-range Follow-up Accuracy 达标，SSE 合同不变。

### V2-G：灰度、治理与可选语义增强

- 按租户/应用灰度；
- Langfuse 看板与错误回放；
- 后台 Episode 摘要质量治理；
- 仅在关键词+元数据不足时评估 Embedding；
- 只有真实多跳关系需求出现后再评估图数据库。

---

## 32. 风险

| 风险 | 表现 | 缓解 |
|---|---|---|
| False Recall | 错接历史主题导致错误 SQL | 高阈值、margin、澄清优先、金标评测 |
| Context Pollution | 旧筛选/指标泄漏到新任务 | Topic Action 先于 Rewrite；Delta 白名单合并 |
| 作用域泄漏 | 跨用户/应用/模型召回 | ACL 硬过滤，存储层强制 scope，负向测试 |
| Event 与 Episode 不一致 | 投影失败或重复 | event_id 幂等、projector offset、可重建 |
| 结果已过期 | Episode 命中但数据不可读 | 元数据仍可见，明确重新执行，不替换成其他结果 |
| 排序不稳定 | “第二个”随执行变化 | 保存 stable_sort_spec、snapshot、data_as_of |
| 延迟上升 | 每轮全局搜索 | Local/Global 两级，热缓存，只有触发才 Global |
| Token 上升 | 召回原文过多 | 结构化候选摘要，最多 1~3 个，按 event_id 局部取证 |
| 模型不稳定 | LLM 同时决定太多 | 确定性候选与阈值，LLM 仅处理边界语义 |
| Schema 演进 | 历史 Snapshot 不兼容 | schema_version、语义资产版本、迁移适配 |
| 双重 Pending | LangGraph interrupt 与现有 CAS 冲突 | 只保留一个权威 Pending 状态源 |
| 数据量增长 | Event/Embedding 成本 | 生命周期、冷热分层、摘要索引、结果内容继续 MinIO |
| 过度架构化 | Manager 和存储重复 | 只新增四个核心模块，复用现有组件 |

---

## 33. ASCII 最终架构图

```text
                                      ┌──────────────────────────────┐
                                      │       Semantic Memory        │
                                      │  偏好 / 别名 / 业务上下文    │
                                      │  复用 LongTermMemory(MySQL)  │
                                      └──────────────┬───────────────┘
                                                     │
User / Platform                                     ▼
      │                           ┌───────────────────────────────────┐
      ▼                           │          Context Builder          │
┌───────────────┐                 │ Immediate + Active Thread         │
│ FastAPI + ACL │                 │ + Retrieved History + Semantic   │
└───────┬───────┘                 │ + Relevant Result Reference      │
        │                         └────────────────┬──────────────────┘
        ▼                                          │
┌───────────────────┐                              ▼
│ Append Turn Event │                 ┌──────────────────────────────┐
└─────────┬─────────┘                 │ Existing Semantic/SQL/Skill  │
          ▼                           │ ASL -> Read-only SQL -> Tool  │
┌───────────────────┐                 │ -> Analysis -> Insight       │
│ Turn Classification│                └──────────────┬───────────────┘
│ current/history/new│                               │
│ ambiguous/correct/ │                               ▼
│ clarification      │                 ┌──────────────────────────────┐
└─────────┬─────────┘                 │ Validation + Evidence + SSE  │
          ▼                           └──────────────┬───────────────┘
┌─────────────────────────┐                         │
│ Level 1 Local Resolution│                         ├───────────────┐
│ Active Thread / State   │                         │               │
│ Recent Turns / Result   │                         ▼               ▼
└───────────┬─────────────┘              ┌────────────────┐  ┌─────────────────┐
            │ low confidence /           │ Result Artifact│  │ Episode Projector│
            │ historical cue             │ MinIO content  │  │ Thread/Episode/  │
            ▼                            │ + lineage meta │  │ Snapshot material│
┌─────────────────────────┐              └───────┬────────┘  └────────┬────────┘
│ Level 2 Global Recall   │                      │                    │
│ Events / Threads /      │                      │                    │
│ Episodes / Results      │                      │                    │
│ metadata+BM25+semantic  │                      │                    │
└───────────┬─────────────┘                      │                    │
            ▼                                    │                    │
┌─────────────────────────┐                      │                    │
│ Rerank + Risk Gate      │                      │                    │
│ top1 / top2 / margin    │                      │                    │
└───────────┬─────────────┘                      │                    │
            ▼                                    │                    │
┌─────────────────────────┐                      │                    │
│ Reference Resolver      │◄─────────────────────┘                    │
│ Topic/Result/Ordinal/   │                                           │
│ Entity/Temporal         │                                           │
└───────┬─────────┬───────┘                                           │
        │ certain │ ambiguous                                          │
        ▼         ▼                                                    │
┌──────────────┐ ┌────────────────────┐                                │
│ State Restore│ │ Existing Pending   │                                │
│ Snapshot     │ │ Clarification CAS  │                                │
│ + Turn Delta │ └────────────────────┘                                │
└───────┬──────┘                                                       │
        └──────────────────────► Context Builder                        │
                                                                       │
┌──────────────────────────────────────────────────────────────────────┴──┐
│ Durable Conversation History (MySQL)                                   │
│ Events ──► Threads ──► Episodes ──► State Snapshots ──► Result Metadata │
│ Redis: active thread / pending / hot cache / DAG checkpoint / trace     │
└─────────────────────────────────────────────────────────────────────────┘

All stages ──► Langfuse / Logging:
classification, recall trigger, candidates, scores, selection,
snapshot, restored state, delta, merged state, result lineage, skills
```

---

## 最终结论

最合适的升级方向不是“增加更多最近消息”或“换一个更强的 LLM”，而是把当前单活动状态架构补齐为：

```text
Durable Conversation Events
  -> Thread / Episode / Snapshot Materialization
  -> Local + Global Hybrid Retrieval
  -> Reference Resolution + Risk Gate
  -> Historical State Restore + Current Delta
  -> Existing Semantic Query / Tool / Analysis Pipeline
  -> Immutable Result Artifact Lineage
```

建议实施时优先完成 V2-C 的结果版本选择和 V2-B 的持久事件/Episode 基础，再以 Shadow Mode 上线 Historical Retrieval。这样既能快速解决截图中的 22→5→10 问题，也不会为长期记忆改造冒险重写当前稳定的数据查询与分析链。

本报告完成后停止；待确认再进入实施阶段。
