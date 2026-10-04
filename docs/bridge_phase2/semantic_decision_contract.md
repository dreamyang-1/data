# 第二阶段：统一语义决策合同

## 目标与边界

生产主线继续固定为 `V2_CONTEXT_V1_EXECUTION`。本阶段不扩大纯 V2 执行范围，
只在 V2 上下文规划与现有 V1 执行之间增加一个进程内、不可由 HTTP 调用方构造的
`SemanticDecision` 合同。

合同用于消除同一轮中重复的改写和意图模型调用。V2 已形成完整、当前授权范围内的
逻辑计划时，V1 只做合同校验、已有业务安全门禁和数据执行；V2 仅完成上下文或计划
不能无损适配时，系统明确进入原有 V1 语义链，并记录降级原因。

## 合同内容

| 内容 | 合同字段 | 约束 |
|---|---|---|
| 对话状态 | `conversation_state` | 来自 V2 当前轮关系，不从 V1 历史重新推断 |
| 问题 | `original_question`、`completed_question` | 同时绑定 message、conversation、application |
| 子任务 | `tasks` | 每项保存问题、主/次意图、参数和依赖 |
| 语义参数 | `metrics`、`dimensions`、`fields`、`filters`、`time_range`、`business_objects` | 完整任务中的字段必须拥有 Catalog ID、编码、版本、模型、域及解析来源 |
| 授权证明 | `scope_proof` | 保存当前请求的不可变 Semantic Scope 指纹及 V2 计划/目录指纹 |
| 澄清 | `clarification_needed`、`clarification_reason` | 需要澄清的合同不能跳过 V1 语义模型 |
| 降级 | `status`、`fallback_reason` | 不支持、低置信或校验失败均显式进入 V1 |

`ChatRequest._semantic_decision` 是 Pydantic `PrivateAttr`。它不会进入请求 Schema、
OpenAPI、SSE 或普通响应序列化，平台调用方不能提交伪造的语义绑定或授权证明。

## 执行规则

只有同时满足以下条件时，V1 才跳过问题改写模型和意图识别模型：

1. 来源为 `V2_AUTHORIZED_PLAN`，状态为 `ACCEPTED`；
2. 当前 message、conversation、application、完整问题与合同完全一致；
3. 合同 Semantic Scope 与当前可信请求完全一致；
4. V2 `LAST_REQUEST` artifact 的 context 与计划 permission requirement 完全一致；
5. 只有一个完整任务，且所有语义字段来自同一授权模型和目录版本；
6. 显式业务域下，每个字段均属于当前授权域；
7. V1 必填槽位在无二次意图模型的情况下仍然齐全。

通过后，指标 ID 使用 V1 既有的 `semantic_model_id:canonical_code` 形式；V2 已验证的
实体值筛选会转换为现有 `SemanticFilterBinding`，继续由 Oagnet/ASL 和结果安全门禁
核对。V1 的任务执行、SQL、结果校验、洞察、MCP 和图表链路保持原样。

## 显式降级

典型原因包括：

| 原因 | 含义 |
|---|---|
| `V2_CONTEXT_ONLY_REQUIRES_V1_SEMANTICS` | V2 只形成完整问题，没有授权逻辑计划 |
| `V2_PLAN_SCOPE_MISMATCH` | 计划授权范围与当前请求不同 |
| `V2_PLAN_ARTIFACT_CONTEXT_MISMATCH` | artifact context 与计划证明不一致 |
| `V2_FILTER_BOOLEAN_GROUP_NOT_ADAPTED` | 当前 V1 适配器不能无损表达该布尔筛选 |
| `V2_MULTI_ENTITY_FILTER_NOT_ADAPTED` | 当前 V1 筛选证明不能无损表达同字段多个实体值 |
| `SEMANTIC_DECISION_*_MISMATCH` | 请求身份、问题或 Scope 在交接后发生变化 |
| `SEMANTIC_DECISION_V1_REQUIRED_SLOT_MISMATCH` | V2 计划形状不能满足当前 V1 执行必填合同 |
| `MULTI_TASK_REQUIRES_V1_SEMANTIC_CLASSIFICATION` | 多任务由现有 V1 拆分器形成，逐任务语义继续走 V1 |

降级不是失败响应。它恢复原有 V1 改写、意图、任务拆分和执行行为，并把原因写入
`semantic.contract.validation` 与 `bridge.v1_execution` 的无内容计时属性。

## 可观测性

`GET /v1/data-analysis/diagnostics/bridge-profile` 返回
`semantic_decision_version=semantic-decision-v1` 和 `semantic_handoff` 策略。
每轮 `performance_trace.operations` 增加：

- `semantic.contract.build`：V2 结果转换为合同；
- `semantic.contract.validation`：V1 消费前的请求、授权和执行就绪校验。

成功复用 V2 计划时，轨迹应包含 `semantic.contract.validation` 且不包含
`v1.question_rewrite`、`v1.intent_recognition`。显式降级时，轨迹会保留
`fallback_reason`，并继续出现相应 V1 操作。

## 当前限制

- V2 Context 当前每轮只向桥接器交付一个最终计划；V1 识别出的复合任务会记录逐任务
  意图、参数和依赖，但仍由原有 V1 子任务链执行。
- 当前 V1 筛选证明只支持每个筛选一个已验证实体值。无法无损映射的筛选明确降级，
  不猜测、不静默丢字段。
- Oagnet、SQL 翻译、SQL 执行、分析模型和 MCP 等真实上游耗时不会因本合同消失；
  本阶段只减少已被 V2 完整证明时的重复 V1 语义模型等待。

## 验证结果

- 合同、计时、V2 Bridge 及原 V2 canonical bridge 专项：**121 passed**。
- 冻结的 V1 多轮、Semantic Scope、结构范围及单域集成：**159 passed**。
- 全量离线回归：**3881 passed / 91 existing failed / 0 collection errors**。
- 与第一阶段基线逐 node id 对照：**old-pass → new-fail = 0**，
  **old-fail → new-pass = 0**；新增 12 项测试全部通过。
- `compileall`、`git diff --check` 和源目录到版本库逐文件 SHA-256 校验通过。

全量 V1 冻结回归显式设置 `DATA_AGENT_RUNTIME_MODE=V1`，避免开发机 `.env` 中的候选
运行模式影响没有声明 runtime 的历史 API 测试；8088 真实验证单独运行在
`V2_CONTEXT_V1_EXECUTION`。

8088 只读实调结果：

| 场景 | 结果 | 合同与调用证据 |
|---|---|---|
| 已发布指标定义 | `COMPLETED` | V2 合同 `ACCEPTED`；无 V1 rewrite、intent、task decomposition；保留 V1 指标发布安全校验 |
| 地区、产品、最近一年趋势 | `COMPLETED` | V2 返回 `V2_SOURCE_VALUE_NOT_FOUND`；明确进入 V1，完整查询与分析链仍成功 |
| 两个独立关系明细查询 | `COMPLETED`，2 个 task result | V1 拆分为两项；合同记录 `MULTI_TASK_REQUIRES_V1_SEMANTIC_CLASSIFICATION` |

三次实调的首个可见内容均约 0.25 秒；相邻可见事件最大间隔分别约 1.02 秒、
2.25 秒和 1.05 秒。原始业务数据、SQL、凭据和完整响应只保存在未提交的私有验证目录。
