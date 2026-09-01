# 首版代码说明

> 现有平台知识库、Milvus 与 ASL 接入的最新状态见
> [PLATFORM_INTEGRATION_STATUS.md](./PLATFORM_INTEGRATION_STATUS.md)。

当前版本是可运行首版：业务数据仍只通过平台 Oagnet 与语义层接口读取；智能体不直连业务库。MySQL 仅用于智能体自己管理已确认的长期偏好。

## 已实现

- 版本化 Pydantic 领域模型和完整主意图枚举。
- 规则安全基线 + 可选结构化模型意图识别、自然日期归一化、槽位检查、多轮追问与会话隔离；追问会回显已理解槽位，并能识别新问题打断旧 Pending。
- 确定性 LangGraph 入口，业务执行权收敛到 Orchestrator。
- Semantic、Policy、Query、Knowledge 强类型 Adapter 契约。
- 指标绑定、知识核验、证据包、可靠性硬门禁和安全兜底。
- 已接入确定性分析执行层：趋势首尾变化、双期间对比、占比、稳健异常候选、贡献候选、线性趋势基线预测、报表数值摘要和结果集空值/重复检查；每次计算生成 `ANALYSIS_RESULT` 证据。数据截断、超限、非数值、负值占比、基期为零、样本不足或归因缺少贡献字段时 Fail Closed。
- 归因只输出数据贡献候选和未验证的知识库因素，不宣称因果成立；预测明确标注为本地线性基线，尚不能替代算法团队发布并回测的生产时序模型。
- JSON API、SSE API、存活和就绪检查。
- Redis 两小时短期结构化记忆、Java `history` 冷恢复，以及带完整请求指纹的幂等缓存；相同消息 ID 携带不同请求时明确返回 HTTP 409。
- MySQL 长期偏好候选、确认、替代、查询和软删除；`agent_long_term_memory` 两个迁移已在当前开发库完成。
- 默认 Mock Adapter 和自动化测试。

当前回归基线为 281 项测试全部通过。除会话、幂等、输入和接口测试外，已覆盖趋势变化率、时间乱序/重复拒绝、多数值列歧义拒绝、零基期与多分组对比门禁、占比负值拒绝、零 MAD 微小噪声、预测样本/非负约束与拟合度、归因贡献字段门禁、非数值拒绝、质量扫描，以及截断结果在编排层 Fail Closed；这仍不替代真实接口契约测试、算法回测和脱敏业务金标评测。

## 本地运行

```powershell
cd E:\YouoAgent\DataAnalysis_Agent
python -m pip install -e ".[dev]"
uvicorn app.main:app --host 127.0.0.1 --port 8088
```

Swagger：`http://127.0.0.1:8088/docs`

请求必须携带由受信网关注入的 `X-Tenant-Id`、`X-User-Id` 和 `X-Application-Id`，Body 同时传同值 `application_id`。本地测试可暂时不传应用 Header；生产环境必须由网关覆盖并删除客户端伪造的同名 Header。

长期记忆管理接口：

- `POST /v1/data-analysis/memory/candidates`：创建待用户确认的偏好。
- `POST /v1/data-analysis/memory/{memory_id}/confirm?application_id=...`：确认并生效；同逻辑键旧偏好自动失效。
- `GET /v1/data-analysis/memory?application_id=...&status=active`：查看记忆。
- `DELETE /v1/data-analysis/memory/{memory_id}?application_id=...`：软删除并停止使用。

只有 `active` 记忆会进入问数链。原始问答、SQL 和数据行不会进入长期记忆。

## 启用结构化意图模型

服务默认使用项目 `New_Agent/.env` 中的共享 `API_KEY` 调用结构化意图模型，不复制密钥；DataAnalysis Agent 自己的 `.env` 可覆盖该引用：

```dotenv
DATA_AGENT_INTENT_MODEL_ENABLED=true
DATA_AGENT_INTENT_MODEL_BASE_URL=https://your-model-service.example.com/v1
DATA_AGENT_INTENT_MODEL_API_KEY=由Secret Manager注入
DATA_AGENT_INTENT_MODEL_NAME=qwen3.6-plus
DATA_AGENT_INTENT_MODEL_RESPONSE_FORMAT=json_object
DATA_AGENT_INTENT_MODEL_TIMEOUT_SECONDS=30
DATA_AGENT_INTENT_MODEL_ENABLE_THINKING=false
DATA_AGENT_INTENT_MODEL_MIN_CONFIDENCE=0.80
```

Qwen3.6-Plus 使用 `/chat/completions` 的 `response_format.type=json_object`，返回后由 Pydantic 严格校验；支持严格 Structured Outputs 的其他模型可配置为 `json_schema`。模型只生成候选结构，服务端仍执行 Schema 校验、置信度门禁、预测/明细确定性约束和槽位校验；超时、非 2xx、空响应、非法 JSON 或 Schema 不匹配时自动降级到规则分类，不把原始输出交给执行器。

## 生产发布前仍需完成

1. Query 服务继续完成 `generate → AST guard → execute` 的安全闭环；智能体不接受客户端 SQL。
2. Java 网关接入可信应用 Header，并提供最近 20 条标准 `history`。
3. 长期偏好需要前端确认/查看/删除交互，自动提取器上线前不得静默写入。
4. 完成 MySQL 备份恢复、迁移回滚、连接容量与故障演练。
5. 算法团队提供版本化预测/异常/归因工具接口、训练数据窗口、特征、模型版本、回测指标和漂移状态；本地确定性执行器仅作为可审计基线。各能力分别通过 Gate B 后再对生产用户开放。
6. ASL/SQL 结果需要补充明确的时间排序、基期/对比期角色、维度贡献字段、完整性与快照字段；智能体不能根据任意列顺序猜测分析语义。
# 平台知识库读取工具

`app/tools/knowledge_base.py` 提供只读异步工具 `KnowledgeBaseSearchTool`，用于调用平台现有的
`POST /knowledge_base/search_docs` 接口。工具不会修改知识库服务代码，也不会上传、更新或删除文档。

调用方必须从可信的登录态/应用配置中取得当前用户获准访问的知识库标识，并在每次请求范围内创建工具：

```python
from app.config import get_settings
from app.tools import build_knowledge_base_search_tool

tool = build_knowledge_base_search_tool(
    get_settings(),
    allowed_knowledge_bases=user_authorized_kb_names,
)
result = await tool(
    query="分析华东区销售额变化",
    knowledge_base_names=["KB_USER_SALES"],
    top_k=30,
    retrieval_method="hybrid",
)
```

`allowed_knowledge_bases` 不能采用模型生成值或未经鉴权的前端参数。模型只能在这个可信白名单内选择知识库。
返回结果包含标准化的 `page_content`、`score`、`metadata`、来源知识库和向量块 ID，可直接进入后续分析或证据链。

相关环境变量见 `.env.example`：

- `DATA_AGENT_KNOWLEDGE_BASE_URL`
- `DATA_AGENT_KNOWLEDGE_BASE_API_KEY`
- `DATA_AGENT_KNOWLEDGE_BASE_SEARCH_PATH`
- `DATA_AGENT_KNOWLEDGE_BASE_TIMEOUT_SECONDS`
