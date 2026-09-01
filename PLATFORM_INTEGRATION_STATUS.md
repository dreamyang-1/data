# 现有平台接入状态

更新日期：2026-08-20

## 已确认的平台能力

`information_safety_test` 已具备文档/文本块入库链路：

```text
/knowledge_base/analysis_vectorize_docs 或 text_chunk_vectorization
→ 文档解析与切片 → Redis 队列 → consume_block_vectorize
→ kb.do_add_doc → Embedding → Milvus
```

`POST /knowledge_base/search_docs` 和 `POST /kb/retrieve` 用于召回 DDL、字段说明、
Q-SQL 和指标文档，不能代替业务数据库的实时数值查询。

`yuyiceng` 记录的 `POST /api/ast-to-sql` 可翻译 ASL 并执行；当前仍缺正式 ASL
JSON Schema、身份权限契约和带数据来源的结果 Schema。

## Agent 已完成的适配

- 新增 `http` Adapter 模式，Mock 模式仍可独立演示。
- Semantic、Policy、Knowledge、Query 均通过独立 Adapter 调用，不直连生产数据库。
- Knowledge Adapter 只检索配置的知识库白名单，证据仅保留来源、分数和块 ID。
- Query Plane 按 `generate → guard → execute` 执行，Guard 未批准即停止。
- 查询结果缺少 `snapshot_id` 或 `data_as_of` 时拒绝回答。
- 外部超时、拒绝或契约错误会转成 `SAFE_FALLBACK`。
- 有幂等键的受控调用支持有限指数退避重试。
- 兼容 `/api/ast-to-sql`，但默认关闭，且生产环境禁止启用。
- 知识命中必须明确 `verified=true` 才通过可靠性门禁。
- Redis 会话已按租户、用户、应用和会话隔离，使用 2 小时 TTL、Lua CAS 和请求指纹幂等；同一 `message_id` 更换请求内容会返回 `409 MESSAGE_ID_REUSE_CONFLICT`，不会串答。
- 指纹协议使用 Redis 前缀 `youo:data-analysis:v2`，与旧版无指纹缓存隔离；旧测试会话等待原 TTL 自然过期。
- 追问已支持自然日期归一化、已理解槽位回显、自定义指标候选（仍须语义层核验）以及显式新任务打断旧 Pending。
- 聊天请求已支持最近 20 条标准 `history`，Redis 过期后可恢复追问和补槽。
- 已新增并实际迁移 MySQL `agent_long_term_memory`，提供候选、确认、替代、软删除和幂等接口；业务知识 Milvus 与用户长期偏好保持分离。

## 启用真实接口

```env
DATA_AGENT_ADAPTER_MODE=http
DATA_AGENT_SEMANTIC_BASE_URL=http://semantic-service.test
DATA_AGENT_POLICY_BASE_URL=http://policy-service.test
DATA_AGENT_QUERY_BASE_URL=http://query-service.test
DATA_AGENT_KNOWLEDGE_BASE_URL=http://knowledge-service.test
DATA_AGENT_KNOWLEDGE_BASE_NAMES=["KB_SALES_DDL","KB_SALES_METRIC"]
DATA_AGENT_QUERY_CONTRACT_MODE=query_plane
DATA_AGENT_PLATFORM_API_KEY=<由Secret Manager注入>
```

现有 ASL 服务仅限隔离测试环境临时验证：

```env
DATA_AGENT_QUERY_CONTRACT_MODE=legacy_asl
DATA_AGENT_LEGACY_ASL_MODEL_ID=<已登记模型ID>
DATA_AGENT_ALLOW_LEGACY_ASL_EXECUTE=true
```

## 尚未完成的外部依赖

1. Java 提供正式 Semantic 和 Policy OpenAPI。
2. Java/数据平台拆分 Generate、Guard、Execute，或提供等价安全保证。
3. Java 提供 `data_source_id/modelId/semantic_model_id/tenant_id` 的服务端映射。
4. Query Execute 返回 `columns`、`rows`、`snapshot_id`、`data_as_of` 和质量状态。
5. 知识库接口落实租户鉴权，不能仅凭知识库名称授权。
6. 提供数据库登记后抽取 Schema/DDL 并调用向量化接口的上游契约。
7. 修复知识库入库链路的后台参数、文件名、标签丢失、消息确认和任务状态问题。
8. Java 在调用聊天接口时传稳定 `application_id`、最近 20 条 `history`，并由网关注入匹配的 `X-Application-Id`。
9. 前端接入长期偏好的确认、列表和删除交互；未确认候选不能影响问数。

## 当前效果

- Mock：可演示意图识别、追问、指标查询、证据、置信度和长短期记忆。
- HTTP：已能按正式安全顺序调用平台接口；不完整响应会被拒绝。
- 真实业务查询：等待 Semantic、Policy、Query 和数据映射契约后联调。
- 基础设施：当前 Redis 与 MySQL 长期记忆 Readiness 均已实连通过。
- 生产发布：尚未达到，网关可信身份、权限、长期记忆 UI、审计、压测和恢复仍未闭环。
