# DataAnalysis_Agent 代码审查问题清单

> 审查范围：`/root/yyy/DataAnalysis_Agent/app/**`（不含 `tests/`）
> 审查方式：静态阅读源码，未做运行时验证；未修改任何代码
> 严重度定义：P0 阻断/安全违背｜P1 行为正确性高｜P2 行为正确性中｜P3 健壮性/可观测性｜P4 风格/边界

## 2026-08-20 复核与处理结果

本节基于当前工作区源码和运行测试复核；下面的原始清单保留，便于追溯，但不能再直接视为当前事实。

### 已确认并修复

| 编号 | 处理结果 |
|---|---|
| P2-1 | 分析算法拒绝数据时，`SAFE_FALLBACK` 现在保留已经取得的查询、指标和知识证据，便于审计。 |
| P2-2 | 确定性分析警告现在强制追加到用户可见答案；Qwen 总结也不能隐藏这些限制。 |
| P2-6 | SSE 在提交 HTTP 200 和发送 `accepted` 前完成执行与幂等校验；冲突、超时分别保持 HTTP 409/504，意外故障为脱敏 HTTP 500。 |
| P3-1/P3-2 | `AdapterError` 增加 `status_code`、`upstream_code`、结构化 `details`；4xx 尽量保留上游稳定错误码。 |
| P3-6 | 澄清轮次超限增加不含原始问题和敏感数据的结构化告警日志。 |
| P3-7 | ASL/SQL 歧义通过结构化 `details` 传递，不再依赖解析异常字符串。 |
| P3-11 | SSE 未接受前的内部异常返回脱敏 HTTP 500，不再把本地代码错误伪装成上游 503。 |
| P3-13 | 常数序列的 `R²` 改为“不适用”，并明确披露常数外推的限制，不再报告虚假的完美拟合。 |
| P3-17 | 语义服务只有明确 `NOT_FOUND/AMBIGUOUS` 才返回空结果；失败或非法响应抛出适配器错误。 |
| P3-18 | ASL 与 SQL 两阶段使用不同的幂等键后缀，避免跨操作碰撞。 |
| P4-1 | `Decimal` 不再经过 `float` 转换，保留原始精度并单独处理非有限值。 |
| P4-2 | 时间序列增加 ISO 周标签支持及合法周校验。 |
| P4-3 | 数据指纹只由规范化列和数据行生成，同一数据在请求重试时保持稳定。 |
| P4-9 | 需要分组标签的分析不再静默使用 `1/2/3`，而是安全拒绝无业务标签的数据。 |

对应专项回归目前为 `155 passed`。

### 经复核不应按原建议修改

- **P1-1**：不能写死文档中的固定 API Key。当前信任边界是 Java 网关注入身份和应用上下文；若要增加服务间认证，应由运维提供独立 Secret、mTLS 或网关签名契约，不能把共享密钥写进源码。
- **P3-4**：`qwen3.6-plus` 已在当前环境完成真实调用和 200 条评测，不是虚构模型名。
- **P3-9**：Kubernetes readiness 默认只反映本实例能否安全接流量；把所有共享下游故障直接传导为实例摘流可能造成级联故障。端到端能力应另设 capability/synthetic health，而不是简单塞进 readiness。
- **P4-4**：角色会影响数据可见范围，必须进入幂等指纹；角色变化后复用旧结果反而可能造成越权。
- **P4-7/P4-8**：`TimeRange` 当前明确为 `date` 粒度；`RedisSessionStore.from_url` 的参数也已完整核验，与调用方一致。
- **P4-10**：占比拒绝负数而趋势/对比允许负数是不同数学语义，不属于口径不一致。

### 确认存在，但需要外部契约或单独架构决策

- **P0-1/P0-2/P0-3/P0-4**：仓库中的五能力 Handshake、Policy、统一 Query/Analysis 契约是一套未完成的目标设计，与当前实际三适配器链路不一致。当前又明确暂不建设权限层，实际生产分析仍采用本地确定性 `AnalysisEngine`。在 Policy、Query、Analysis 服务的真实 URL、鉴权、请求/响应 Schema 和健康端点确定前，不能生成假适配器并阻断现有链路。
- **P2-4/P2-5**：`adapters/errors.py` 和 `stores/redis.py` 是未接入的旧实现；直接删除可能影响仍引用它们的外部分支/测试。应在代码分支收敛后做一次兼容迁移再删除。
- **P3-3/P3-19**：`contracts.py` 是目标契约，而当前 HTTP Adapter 对接的是现有 Oagnet、语义 SQL 和 `information_safety_test` 知识库接口。需要接口提供方确认最终契约后统一，不能单方面改路径。
- **P3-14/P3-15/P3-16**：异常阈值、多对象比较和归因字段识别需要算法/语义层给出统计口径与字段元数据；当前继续 Fail Closed，避免猜测。

### 测试仓库自身的不一致

当前全量收集还存在三项与本轮修复无关的问题：`tests/test_contracts.py` 引用了尚不存在的 `HttpPolicyAdapter/HttpQueryAdapter`，`tests/test_handshake.py` 引用了尚不存在的 `WorkflowNodes`，原开发依赖未声明 `fakeredis`。本轮已经在 `pyproject.toml` 补充 `fakeredis` 开发依赖，当前 Python 环境仍需重新安装开发依赖后才会生效；前两项必须随“目标五能力架构”是否正式启用一并收敛，不能通过添加空类来制造假通过。

### 二次复核（修复验证）

在上一节“复核与处理结果”落地 14 项修复后，对相同代码再做一次完整复核。下表记录修复验证结论与本轮新发现的剩余问题。

**修复验证通过（14 项）**

| 编号 | 验证结论 |
|---|---|
| P2-1 | `orchestrator._handle` 在 `AnalysisError` 路径上通过 `fallback.evidence = evidence` 保留证据，证据链不丢失。 |
| P2-2 | `analysis_output.warnings` 现以“注意事项：”前缀强制追加到用户可见 `answer`，Qwen 总结无法再隐藏。 |
| P2-6 | `api.py` 的 SSE 路由在 `invoke()` 成功后再 yield `accepted` 事件，message_id 复用与超时检查先于 `accepted` 完成，HTTP 状态码与事件不再错位。 |
| P3-1/P3-2 | `AdapterError` 增加了 `status_code`、`upstream_code`、结构化 `details` 字段；`http.py` 的 4xx 分支按 `error_code → code → status_code` 顺序填充 `upstream_code`。 |
| P3-6 | `_request_clarification` 在超过 `max_clarification_rounds` 时输出结构化 `logger.warning`，不泄漏用户原文。 |
| P3-7 | `_ambiguity_texts` 优先读取 `exc.details`（list/dict 均可），JSON 解析仅作 fallback。 |
| P3-11 | SSE 内部错误返回 HTTP 500，不再伪装成上游 503。 |
| P3-13 | 常数序列下 `r_squared = None`，并在结论和 warnings 中分别显式披露“R² 不适用”，不再误导。 |
| P3-17 | `HttpSemanticAdapter.resolve_metrics` 仅在 `NOT_FOUND`/`AMBIGUOUS` 时返回空，其它错误抛 `AdapterError`。 |
| P3-18 | ASL 与 SQL 调用的幂等键分别追加 `:asl`/`:sql` 后缀，不再冲突。 |
| P4-1 | `_number` 对 `Decimal` 原样返回，不经过 `float` 转换。 |
| P4-2 | `_time_key` 新增 ISO 周格式正则解析，`date.fromisocalendar` 失败返回 `None`。 |
| P4-3 | `_dataset` 的 `fingerprint` 基于规范化后的列与行生成，不再包含 `request_id`，保证幂等。 |
| P4-9 | `_labels` 对缺标签列直接 `raise`，不再退化为数值索引列名。 |

**本轮新发现的剩余问题（审核报告未列入三分类）**

下列问题既未出现在“已确认并修复”清单，也未列入“不应按原建议修改”或“需外部契约”清单，属于上一轮复核遗漏的开放问题。本轮不做任何代码修改，仅记录以待后续处理。

| No. | 问题 | 严重度 | 证据 |
|---|---|---|---|
| R-1 | **P2-3 加重：生产环境 reliability.level=HIGH 不可达** —— `http.py` 在 `_dataset` 中硬编码 `quality_status="UNVERIFIED_SOURCE_SNAPSHOT"`；`orchestrator._reliability()` 对 `quality_status != "PASS"` 一律追加一条“上游数据质量状态…结论需要复核”的 warning；而 `level="HIGH" if score == 1 and not warnings else "LIMITED"`。HTTP 模式下 warning 恒非空 → HIGH 恒为 False。仅 mock 模式（`Dataset.quality_status` 默认 `PASS`）下 HIGH 可达。监控若按 `reliability.level==HIGH` 判定基线将永远失败。 | 高 | `app/adapters/http.py` `_dataset`；`app/services/orchestrator.py` `_reliability()` |
| R-2 | **P3-1/P3-2 部分修复：AdapterError 新字段未被消费** —— `status_code`/`upstream_code` 在 `http.py` 中正确填充，但 `orchestrator._dependency_message(exc.code)` 仍只读 `exc.code`，新字段在仓库中没有任何消费方（`exc.status_code`/`exc.upstream_code` 无任何引用）。结构化数据当前为死数据，仅在未来被消费时才有价值。 | 中 | `app/services/orchestrator.py` `_dependency_message`；`app/adapters/base.py` `AdapterError` |
| R-3 | **P3-5：knowledge_score_threshold 默认 1.0** —— `config.py` 仍为 `default=1.0`。在 cosine 0~1 量纲下可能过滤掉绝大多数检索结果；本地不二次过滤，阈值行为完全依赖上游实现，变更效果不可预测。 | 低 | `app/config.py` `knowledge_score_threshold` |
| R-4 | **P3-10：adapter_mode 默认 mock，无 staging guard** —— `config.py` 默认 `adapter_mode="mock"`；`dependencies.py` 仅在 `env=="production"` 时禁止 mock。staging/预发环境无 guard，可能静默返回假数据而无人察觉。 | 低 | `app/config.py` `adapter_mode`；`app/dependencies.py` 容器构造 |
| R-5 | **P4-5：memory_http_error 仍将内部 bug 映射为 503** —— ValueError 已改进为 422，但 KeyError/AttributeError/pydantic ValidationError 等内部代码 bug 仍统一 `return HTTPException(status_code=503, ...)`，客户端无法区分上游故障与本地代码错误。 | 低 | `app/api.py` `memory_http_error` |
| R-6 | **P4-6：custom_openapi 硬编码路径名** —— `main.py` 的 `custom_openapi` 手写 `("/v1/data-analysis/chat", ...)` 等路径串；路径若变更，`required` 标记会静默失效且不报错，OpenAPI 文档与实际路由漂移。 | 低 | `app/main.py` `custom_openapi` |

### 三次复核（对二次审核新增项的处理）

| 编号 | 复核结论与处理 |
|---|---|
| R-1 | **问题部分存在，已修复数据来源而未放宽门禁。** HTTP 查询结果现在会校验并采用上游真实 `snapshot_id`、带时区的 `data_as_of` 和 `quality_status`；三项完整且为 `PASS` 时 HIGH 可达。缺少任一项时继续标记 `UNVERIFIED_SOURCE_SNAPSHOT` 并保持 LIMITED，禁止本地伪造高置信度。 |
| R-2 | **存在，已修复。** `_dependency_message` 现在消费完整 `AdapterError`：按 401/403/404/409/422 输出稳定分类话术；未知错误可携带脱敏后的上游稳定业务码，原始异常文本不返回用户。 |
| R-3 | **证据不足，忽略。** 当前知识库接口文档明确分数范围为 0–2，且“值越小相关度越高”，不能按 cosine 0–1 相似度推断默认 1.0 错误。本地擅自二次过滤还可能与上游距离量纲相反；应由知识库团队给出正式 score 语义后再调整。 |
| R-4 | **不成立，忽略。** `env` 当前只有 development/test/production，没有 staging；production 已硬性拒绝 Mock，`/ready` 也公开返回 `adapter_mode`。若未来增加 staging 枚举，应同时增加对应 guard。 |
| R-5 | **存在，已修复。** 已知不存在/状态迁移/并发冲突/值错误分别映射 404/409/422；未知 `KeyError`、`AttributeError` 等本地异常记录服务端日志并返回脱敏 HTTP 500，不再伪装成 503。 |
| R-6 | **存在，已修复。** OpenAPI 不再硬编码聊天路径，而是遍历所有带 `data-analysis` tag 的实际路由，为聊天和长期记忆接口统一标注可信身份 Header。 |

本轮新增专项测试覆盖：真实快照元数据透传、非法时间戳拒绝、稳定指纹、错误分类消费、长期记忆内部异常脱敏以及动态 OpenAPI Header 标记。

### 四次复核（重启前代码审查）

本轮继续检查 API、Dataset、长期记忆和启动链路，新确认并修复两项边界问题：

1. 长期记忆配置为 `disabled` 时，记忆接口原来会对 `None` 调用方法并落入内部 500；现在统一返回明确、脱敏的 HTTP 503 `long-term memory capability is disabled`。
2. Dataset 原来允许 `NaN`、正负 `Infinity` 和非有限 Decimal 进入，可能污染统计计算或导致 JSON 序列化失败；现在契约层递归拒绝非有限数值，并设置嵌套值数量安全上限。

本轮可运行关键回归基线更新为 `180 passed`。完整测试收集仍受前述未完成五能力目标测试和当前环境尚未安装 `fakeredis` 影响。

**未引入新缺陷**：14 项修复均未在本轮引入新缺陷或回归。原审核报告中标记为 P3-8（单节点 langgraph）、P3-12（列自动选择）的设计选择仍存在，但与前一轮结论一致，且与本轮修复无关，不再重复展开。

---

## 概览

共发现 **40 项问题/风险**，分布如下：

| 严重度 | 数量 | 主要分布 |
|--------|------|----------|
| P0 | 4 | Capability Handshake 未启用 / Policy 适配器缺失 / Handshake 签名错配 / AnalysisAdapter 缺失 |
| P1 | 1 | API 路由缺鉴权（HTTPBearer） |
| P2 | 6 | SAFE_FALLBACK 丢证据 / 警告未进 answer / 重复 AdapterError / 孤儿 RedisSessionStore / SSE 提前 accepted / reliability 过度降级 |
| P3 | 19 | 配置与契约偏离 / 上游错误码丢失 / 重试覆盖不全 / 模型名疑似虚构 / 知识阈值默认值怪异 / 探测点缺失 等 |
| P4 | 10 | fingerprint 语义脆弱 / 单列结果集标签退化 / 闰年/周粒度未覆盖 等 |

最关键的三件事：
1. **生产启动流程没有调用 Handshake，且就算调用也无法工作**（适配器 Protocol 与 `handshake.py` 期望的字段/方法完全错位）。
2. **Policy 适配器与 AnalysisAdapter 在生产代码中完全缺失**，与项目记忆里的硬约束（"METRIC_QUERY/DETAIL_QUERY require semantic+policy+query+knowledge"）直接矛盾。
3. **API 入口没有 HTTPBearer 鉴权**，仅靠"可信 Java 网关注入 X-Tenant-Id/X-User-Id"，任何能直接打到端口的人都可以冒充任意租户/用户。

---

## P0 — 阻断 / 设计违背 / 安全违背

### P0-1. Capability Handshake 完全脱离启动流程

- **位置**：[app/dependencies.py](file:///root/yyy/DataAnalysis_Agent/app/dependencies.py) `build_container()`；[app/main.py](file:///root/yyy/DataAnalysis_Agent/app/main.py) `lifespan()`
- **现象**：`build_container()` 是**同步函数**，全程未调用 `probe_capabilities()`；`main.py` 的 `lifespan` 只 `build_container(settings)` 后 `yield`，同样不 await Handshake。
- **证据**：全工程内 `handshake | probe_capabilities | HandshakeReport` 仅出现在 [app/adapters/handshake.py](file:///root/yyy/DataAnalysis_Agent/app/adapters/handshake.py) 与 [tests/test_handshake.py](file:///root/yyy/DataAnalysis_Agent/tests/test_handshake.py) 两处，生产路径无任何引用。
- **风险**：
  - 项目硬约束"Container initialization (build_container) must be async and await probe_capabilities() to complete Handshake before service startup"完全不生效。
  - "探测失败 → 标记 capability 不可用 → 工作流路由让相关意图 safe_terminate"这一安全承诺不存在；上游故障必须等真实用户请求时才暴露，违反 fail-fast。
  - `/ready` 端点对外声称"ready"但上游能力未探活。
- **建议修复方向**（不在本次实施）：将 `build_container` 改为 `async`，在 lifespan 内 `await probe_capabilities(adapters)` 并把 `HandshakeReport` 挂到 `Container`；`/ready` 增加按能力汇报。

---

### P0-2. Handshake 探测目标与 AdapterBundle 字段/Protocol 方法不一致

- **位置**：[app/adapters/handshake.py#L48-L54](file:///root/yyy/DataAnalysis_Agent/app/adapters/handshake.py) 与 [app/adapters/base.py](file:///root/yyy/DataAnalysis_Agent/app/adapters/base.py)
- **现象**：`probe_capabilities()` 调用：
  ```python
  _probe_one("semantic", adapters.semantic.health),
  _probe_one("policy", adapters.policy.health),       # ← AdapterBundle 无 policy 字段
  _probe_one("query", adapters.query.health),          # ← AdapterBundle 无 query 字段（实为 retrieval）
  _probe_one("knowledge", adapters.knowledge.health),
  _probe_one("analysis", adapters.analysis.health),    # ← AdapterBundle 无 analysis 字段
  ```
  而 `AdapterBundle` 实际只有 `semantic` / `retrieval` / `knowledge` 三个字段；`SemanticAdapter`、`DataRetrievalAdapter`、`KnowledgeAdapter` Protocol 也没有声明 `health()` 方法。
- **证据**：[app/adapters/http.py](file:///root/yyy/DataAnalysis_Agent/app/adapters/http.py) 与 [app/adapters/mock.py](file:///root/yyy/DataAnalysis_Agent/app/adapters/mock.py) 的所有适配器类都没有 `health()` 实现。
- **风险**：只要启用 Handshake，立即 `AttributeError: 'AdapterBundle' object has no attribute 'policy'` 或 `'HttpSemanticAdapter' object has no attribute 'health'`。当前能"工作"仅仅因为 P0-1 让它根本没被调用。
- **建议方向**：在 `base.py` 把 `AdapterBundle` 字段对齐到 5 类（semantic/policy/query/knowledge/analysis），并在每个 Protocol 上声明 `async def health(self) -> bool: ...`。

---

### P0-3. Policy 适配器整体缺失（行列权限 / 字段脱敏 / 查询预算全部跳过）

- **位置**：[app/adapters/contracts.py](file:///root/yyy/DataAnalysis_Agent/app/adapters/contracts.py)（已定义 `AuthorizeRequest`/`AuthorizeResponse`）vs. [app/adapters/http.py](file:///root/yyy/DataAnalysis_Agent/app/adapters/http.py)、[app/adapters/mock.py](file:///root/yyy/DataAnalysis_Agent/app/adapters/mock.py)、[app/config.py](file:///root/yyy/DataAnalysis_Agent/app/config.py)、[app/services/orchestrator.py](file:///root/yyy/DataAnalysis_Agent/app/services/orchestrator.py)
- **现象**：
  - 契约层定义了 Policy 服务端点与 `row_policy` / `field_masking` / `query_budget` 字段；
  - 但 `config.py` **没有 `policy_base_url` 配置项**；
  - `http.py` / `mock.py` **没有** `HttpPolicyAdapter` / `MockPolicyAdapter` 类；
  - `build_http_adapters` 返回 `AdapterBundle(semantic, retrieval, knowledge)` — 三元，未传 policy；
  - `orchestrator.py` 全程未调用任何 `policy.authorize(...)`。
- **风险**：
  - 与项目硬约束"METRIC_QUERY/DETAIL_QUERY require semantic+policy+query+knowledge"直接冲突；
  - 行级过滤（`row_policy`）依赖下游 SQL 服务自己做，本智能体未传递；
  - 字段脱敏规则（`field_masking`）未应用；
  - 查询预算（最大行数/超时/成本上限）未生效，`data_query_max_rows` 仅在本地兜底；
  - 所有 `X-Tenant-Id/X-User-Id/roles` 身份信息只透传到 semantic 与 retrieval，policy 一层未参与，构成数据越权风险。

---

### P0-4. AnalysisAdapter / HttpAnalysisAdapter 缺失，意图能力依赖声明与现实脱节

- **位置**：[app/contracts.py AnalysisRequest/AnalysisResponse](file:///root/yyy/DataAnalysis_Agent/app/adapters/contracts.py#L267-L329) vs. [app/services/orchestrator.py](file:///root/yyy/DataAnalysis_Agent/app/services/orchestrator.py)、[app/adapters/http.py](file:///root/yyy/DataAnalysis_Agent/app/adapters/http.py)、[app/adapters/mock.py](file:///root/yyy/DataAnalysis_Agent/app/adapters/mock.py)、[app/config.py](file:///root/yyy/DataAnalysis_Agent/app/config.py)
- **现象**：项目记忆声明"预测能力基于统一的 AnalysisAdapter 作为编排层……HttpAnalysisAdapter 用于生产"。实际：
  - `orchestrator._handle()` 直接 `self.analysis_engine.analyze(...)`，**本地** `AnalysisEngine` 完成所有 8 种分析；
  - `http.py` 无 `HttpAnalysisAdapter`，`mock.py` 无 `MockAnalysisAdapter`；
  - `config.py` 无 `analysis_base_url`；
  - `contracts.py` 中 `AnalysisRequest`/`AnalysisResponse` 整段契约**没有任何代码使用**（仅在 `CONTRACTS` 注册表中被声明）。
- **风险**：
  - "TREND_ANALYSIS / FORECAST_ANALYSIS 等 require analysis capability"在跨进程层面无契约保障；
  - 本地算法实现变更无版本控制、无独立部署；
  - 一旦未来需要把 FORECAST 切到真实算法服务，需要重写 orchestrator 调用层而不是切换 adapter；
  - Handshake 想探测 analysis 能力时（如 P0-2 修复后）拿不到任何实现可探。

---

## P1 — 行为正确性高

### P1-1. API 路由缺少 HTTPBearer 鉴权

- **位置**：[app/api.py](file:///root/yyy/DataAnalysis_Agent/app/api.py) 全部路由
- **现象**：所有 `/v1/data-analysis/*` 路由仅靠 `X-Tenant-Id` / `X-User-Id` / `X-Application-Id` Header 鉴权（依赖"可信 Java 网关注入"）；服务自身入口**没有任何** `HTTPBearer` 中间件或 API Key 校验。
- **证据**：项目硬约束要求所有 API 路由通过 FastAPI HTTPBearer 校验配置化的 API Key；当前 `api.py` 全文搜索不到 `HTTPBearer`、`Authorization`、`APIKey`。历史文档中的明文凭据已移除。
- **风险**：
  - 任何能直接访问该 FastAPI 端口的人都可以伪造任意 `X-Tenant-Id` / `X-User-Id`，冒充任意租户/用户；
  - 下游所有行权限审计、长期记忆 scope 隔离全部失真；
  - `http.py` 调用上游时使用 `platform_api_key`（Bearer），但**服务自身入口**没有任何鉴权，存在"信任代理"陷阱。
- **建议方向**：在 `api.py` 顶部加 `bearer = HTTPBearer(auto_error=True)`，在路由 `dependencies=[Depends(bearer)]`，与 settings.platform_api_key 比对。

---

## P2 — 行为正确性中

### P2-1. SAFE_FALLBACK 丢失已收集的证据

- **位置**：[app/services/orchestrator.py](file:///root/yyy/DataAnalysis_Agent/app/services/orchestrator.py) `_handle()` 中 `except AnalysisError as exc:`
  ```python
  except AnalysisError as exc:
      return await self._finish_terminal(
          request,
          self._fallback(request, f"数据不足以支持可靠分析：{exc}。"),
      )
  ```
  `self._fallback()` 创建的 `AgentResponse` 使用空 `evidence=[]`。
- **风险**：
  - 此前已经收集的 `QUERY_RESULT`、`KNOWLEDGE_VERIFICATION`、`ANALYSIS_KNOWLEDGE` 全部被丢弃；
  - 用户拿到一句兜底话术，但拿不到这次失败的根因数据；
  - 审计/排障无法从响应回溯用户当时实际查到的是什么数据，违反"答案必须带证据"的核心约束。
- **建议方向**：`fallback = self._fallback(...); fallback.evidence = evidence; return await self._finish_terminal(request, fallback)`。

---

### P2-2. `analysis_output.warnings` 未进入用户可见 `answer`

- **位置**：[app/services/orchestrator.py](file:///root/yyy/DataAnalysis_Agent/app/services/orchestrator.py) `_handle()` 末尾：
  ```python
  answer = (analysis_output.answer
            if analysis_output is not None
            else self._analyze(...))
  ```
- **现象**：`AnalysisOutput.warnings`（如 `"归因结果是候选贡献而非因果证明"`、`"基线预测不是已校准的生产时序模型"`、`"线性趋势拟合度较低，预测不稳定"`）仅放入 `evidence` 的 `payload.warnings`，**不会出现在 `answer` 字段**。
- **风险**：
  - 前端若只渲染 `answer` 字段，用户永远看不到安全免责声明；
  - 业务方极易把 `ROOT_CAUSE_ANALYSIS` 当成因果定论、把 `FORECAST_ANALYSIS` 当成正式预测；
  - 与项目硬约束"答案强制披露规则"矛盾。
- **建议方向**：`answer = analysis_output.answer; if analysis_output.warnings: answer += "\n注意事项：" + "；".join(analysis_output.warnings) + "。"`。

---

### P2-3. `_reliability()` 警告过度降级，HIGH 信号被稀释

- **位置**：[app/services/orchestrator.py](file:///root/yyy/DataAnalysis_Agent/app/services/orchestrator.py) `_reliability()`：
  ```python
  ReliabilityReport(level="HIGH" if score == 1 and not warnings else "LIMITED" ...)
  ```
- **现象**：所有 warnings（含"上游数据质量状态为 X，结论需要复核"这种轻量提示，与"非因果证明"这种关键免责）一律把 HIGH→LIMITED。
- **风险**：
  - 真实场景 HIGH 几乎永远拿不到，运营 dashboard 的 LIMITED 失去区分价值；
  - 关键安全免责与一般 quality_status 被同等对待，关键信号被稀释；
  - SLA/告警基于 reliability.level 触发时，告警频率会显著虚高。
- **建议方向**：区分 critical（含"上游数据质量"、"知识库"、"非因果"）与 advisory warnings；只有 critical 触发降级。

---

### P2-4. 重复定义的 `AdapterError`，`adapters/errors.py` 完全孤儿

- **位置**：
  - [app/adapters/base.py](file:///root/yyy/DataAnalysis_Agent/app/adapters/base.py)：`class AdapterError(RuntimeError)`，签名 `(code, message, *, retryable=False)`；
  - [app/adapters/errors.py](file:///root/yyy/DataAnalysis_Agent/app/adapters/errors.py)：`class AdapterError(Exception)`，签名 `(kind, message, *, source, retryable, status_code, cause)`，并附带 `ErrorKind` 六分类、`classify_http_error()`、`safe_call()` 重试/降级框架。
- **现象**：`errors.py` 全文未被任何模块导入（仅自身函数相互引用）；`http.py` / `orchestrator.py` 全部从 `base` 导入。`http.py` 在 `PlatformHttpClient._request()` 里重新实现重试/降级，与 README §2.6 承诺的五分类不一致。
- **风险**：
  - 上游业务错误码（`ErrorPayload.error_code`）丢失；
  - 重试策略未基于 `ErrorKind`（`NETWORK/TIMEOUT` 应退避重试，`CONTRACT/AUTH` 不应重试），目前 `http.py` 简单按 4xx/5xx 区分；
  - `to_fallback_reason()` 用户可见话术未使用；
  - 维护者读到 `errors.py` 会被误导以为是生效代码。

---

### P2-5. `stores/redis.py` 孤儿且与生产实现 API 不兼容

- **位置**：[app/stores/redis.py](file:///root/yyy/DataAnalysis_Agent/app/stores/redis.py) vs. [app/stores/session.py](file:///root/yyy/DataAnalysis_Agent/app/stores/session.py)
- **现象**：
  - `app/stores/__init__.py` 只 re-export `session.py`，**不**导出 `redis.py`；
  - `redis.py` 中的 `RedisSessionStore.__init__(redis_url, ttl_seconds, client=None)`、`get_pending(tenant_id, conversation_id)`、`put_pending(state)`（无 `expected_version`）签名与 `session.py` 的 `RedisSessionStore` 完全不兼容；
  - Key 前缀也不同：`redis.py` 用 `da:pending:`，`session.py` 用 `session_key_prefix`（默认 `youo:data-analysis:v2`）；
  - 仅 [tests/test_redis_session.py](file:///root/yyy/DataAnalysis_Agent/tests/test_redis_session.py) 引用。
- **风险**：
  - 维护者在重构时极易 `from app.stores.redis import RedisSessionStore`，运行时签名不匹配；
  - 两个 store 的 Key 命名不同，跨版本/跨实例无法互通；
  - CAS 实现各异，行为不同。

---

### P2-6. SSE `/chat/stream` 提前 `accepted` 破坏幂等性契约

- **位置**：[app/api.py](file:///root/yyy/DataAnalysis_Agent/app/api.py) `chat_stream()` `events()`：
  ```python
  yield _event("accepted", {"message_id": payload.message_id})  # 先发
  try:
      response = await invoke(request, payload, identity)        # 再做幂等检查
  except HTTPException as exc:
      yield _event("error", {"status_code": exc.status_code, "detail": exc.detail})
  ```
- **现象**：`StreamingResponse` 开始迭代后 HTTP 状态已提交为 200，无法再变成 409；后续 `invoke()` 若抛 `MessageIdReuseConflictError`（按同步路径应返回 409）只能改成 SSE `error` 事件。
- **风险**：
  - 网关若按 HTTP 状态做幂等去重，会误判"已被接受"，对重复 message_id 不再拦截；
  - 与同步 `/chat` 返回 409 的语义不一致；
  - 客户端 SDK 必须额外解析 SSE 事件才能发现冲突，违反契约一致性。
- **建议方向**：先在 `events()` 入口同步做幂等校验，再发 `accepted`；或让 `accepted` 仅在确认无冲突后发。

---

## P3 — 健壮性 / 可观测性 / 契约一致性

### P3-1. `http.py` 重试未覆盖所有失败模式，丢失上游业务错误码

- **位置**：[app/adapters/http.py](file:///root/yyy/DataAnalysis_Agent/app/adapters/http.py) `PlatformHttpClient._request()`
- **现象**：循环捕获 `(httpx.HTTPError, ValueError)`；非 httpx 异常（如 KeyError、AttributeError、asyncio 局部 bug）会直接外泄；4xx 中只有 401/403/429/>=500 显式处理，其它 4xx（含 404、409、422）一律走 `DEPENDENCY_CONTRACT_REJECTED`，丢失上游 `ErrorPayload.error_code`/`details`。
- **风险**：调用方拿不到上游业务码，无法差异化兜底；排障困难。

---

### P3-2. `base.AdapterError` 缺少 status_code 字段

- **位置**：[app/adapters/base.py](file:///root/yyy/DataAnalysis_Agent/app/adapters/base.py)
- **现象**：`AdapterError(code, message, retryable=False)` 仅含 code/message/retryable；`orchestrator._dependency_message(code)` 用写死的字典映射几个 code，其它一律 `"上游数据服务暂时不可用（{code}）"`。
- **风险**：审计/告警拿不到 HTTP 状态与上游业务码；调用方无法基于 status 决策。

---

### P3-3. 配置默认值多处偏离 `contracts.py` 契约

- **位置**：[app/config.py](file:///root/yyy/DataAnalysis_Agent/app/config.py) vs. [app/adapters/contracts.py](file:///root/yyy/DataAnalysis_Agent/app/adapters/contracts.py)
- **现象**：
  | 契约端点 | 实际配置路径 | 偏离 |
  |----------|--------------|------|
  | `POST /v1/metrics:resolve` | `semantic_resolve_path=/v1/semantic/metrics/resolve` | 路径不同 |
  | `GET /v1/metrics/{id}/definition?version={version}` | `semantic_definition_path=/v1/semantic/metrics/{metric_id}/versions/{version}` | 路径与方法不同 |
  | `POST /v1/metrics/{id}/lineage` (body: tenant_id+user_id) | `semantic_lineage_path=/v1/semantic/metrics/{metric_id}/lineage` (body: version) | body schema 不同 |
  | `POST /v1/knowledge:verify` (body: metric_id+version) | `knowledge_base_search_path=/knowledge_base/search_docs` (body: query+top_k+...) | 完全不同 |
- **风险**：外部团队若按 `contracts.py` 联调会全部失败；契约测试无法发现运行时偏差；`CONTRACTS` 注册表与代码行为偏离，无法用于 Handshake 与文档生成。

---

### P3-4. `intent_model_name = "qwen3.6-plus"` 疑似虚构模型名

- **位置**：[app/config.py](file:///root/yyy/DataAnalysis_Agent/app/config.py)
- **现象**：阿里云 DashScope 当前公开模型 ID 不存在 `qwen3.6-plus`（实际如 `qwen-plus`、`qwen-max`、`qwen3-32b-instruct` 等）。
- **风险**：`intent_model_enabled=True` 时，`StructuredIntentModelClient` 调用必返回 model-not-found，意图识别退化为 Rule fallback；struct 模型承诺未生效。

---

### P3-5. `knowledge_score_threshold` 默认 1.0，本地不过滤

- **位置**：[app/config.py](file:///root/yyy/DataAnalysis_Agent/app/config.py) `knowledge_score_threshold: float = Field(default=1.0, ge=0, le=2)`；[app/adapters/http.py](file:///root/yyy/DataAnalysis_Agent/app/adapters/http.py) `HttpKnowledgeAdapter` 仅 `hits[: top_k]` 切片
- **现象**：阈值发给上游，本地不校验；1.0 在 cosine 0~1 量纲下会过滤掉绝大多数结果，但上游是否真的过滤未知。
- **风险**：知识检索结果集边界完全黑盒；score_threshold 配置形同摆设；阈值变更无效果预测。

---

### P3-6. `max_clarification_rounds` 溢出后丢弃 pending 但无审计

- **位置**：[app/services/orchestrator.py](file:///root/yyy/DataAnalysis_Agent/app/services/orchestrator.py) `_request_clarification()`：`if rounds > max_clarification_rounds: clear_pending(); return fallback(...)`
- **现象**：无日志、无 long_memory 写入。
- **风险**：用户多次澄清仍失败的会话无审计痕迹；难以发现意图分类器在特定 pattern 上的系统性缺陷。

---

### P3-7. `_ambiguity_texts()` 用 `json.loads(str(exc))` 复用错误信息

- **位置**：[app/services/orchestrator.py](file:///root/yyy/DataAnalysis_Agent/app/services/orchestrator.py)
- **现象**：把异常的字符串表示当成 JSON 解析；一旦 `AdapterError` 的 message 不严格 JSON（含中英文标点、上下文前缀），就退化为 `[str(exc)]`。
- **风险**：澄清问题质量不稳定；用户看到的歧义提示可能是一段原始 JSON 字符串。

---

### P3-8. 单节点 langgraph 引入间接但无收益

- **位置**：[app/graph/workflow.py](file:///root/yyy/DataAnalysis_Agent/app/graph/workflow.py) `build_workflow()`
- **现象**：单节点 `deterministic_analysis_workflow` → `orchestrator.handle()`，再 END。
- **风险**：多一层间接、调试与 trace 复杂度增加，没有可观测的图状态收益；维护者期待"图状态"但实际无 state 演化。

---

### P3-9. `/ready` 不探测任何外部上游

- **位置**：[app/main.py](file:///root/yyy/DataAnalysis_Agent/app/main.py) `ready()`
- **现象**：只 ping redis 与 mysql（long_memory）；不探 semantic、asl_generator、sql_translator、knowledge、policy、analysis。
- **风险**：K8s readiness probe 在上游全挂时仍返回 200，流量持续打到本服务然后请求时才失败。

---

### P3-10. 默认 `adapter_mode="mock"` + `env="development"` 无 guard

- **位置**：[app/config.py](file:///root/yyy/DataAnalysis_Agent/app/config.py)、[app/dependencies.py](file:///root/yyy/DataAnalysis_Agent/app/dependencies.py)
- **现象**：未配置 env 的部署会静默以 mock 模式运行，返回 `128000.00` 这类假数据；`dependencies.py` 只在 `env=="production"` 时禁止 mock，dev/staging 无 guard。
- **风险**：测试环境联调时极易拿到"看起来对、其实是假"的数据，掩盖上游真实问题；staging 演练无法暴露真实链路。

---

### P3-11. SSE 异常分支一律 503 "service is temporarily unavailable"

- **位置**：[app/api.py](file:///root/yyy/DataAnalysis_Agent/app/api.py) `events()` `except Exception`
- **现象**：用 `logger.exception` 记录原始异常是好的，但响应给客户端的 detail 完全没有特征。
- **风险**：客户端无法区分超时/上游故障/本地 bug；运维 dashboard 上所有非超时错误被合并，告警误判上游故障。

---

### P3-12. `AnalysisEngine._series` 列自动选择策略脆弱

- **位置**：[app/analysis/engine.py](file:///root/yyy/DataAnalysis_Agent/app/analysis/engine.py) `_series()`
- **现象**：通过 `preferred` 名字匹配 + 排除 id/日期类关键词；多列同时含"金额"、"销售额"等会触发 `raise AnalysisError("存在多个数值指标列")`。
- **风险**：用户得到"ASL/SQL 结果必须明确指标列"错误时无法理解为何系统拿不到列名；兜底失败率高。

---

### P3-13. `_forecast` 在 total_variance==0 时 r_squared=1.0 静默接受

- **位置**：[app/analysis/engine.py](file:///root/yyy/DataAnalysis_Agent/app/analysis/engine.py) `_forecast()`：`r_squared = 1.0 if total_variance == 0 else 1 - residual_variance / total_variance`
- **现象**：所有历史值相等（total_variance == 0）时 slope=0、intercept=mean、forecast=mean，r_squared=1.0 看起来"完美拟合"。
- **风险**：恒定序列的预测无信息量，但报告显示高置信度；与"基线预测不是已校准的生产时序模型"的免责叠加时极易误导。

---

### P3-14. `_anomaly` mad==0 时回退到 `median * 0.5` 阈值

- **位置**：[app/analysis/engine.py](file:///root/yyy/DataAnalysis_Agent/app/analysis/engine.py) `_anomaly()`：`if mad == 0: relative_floor = max(abs(median) * 0.5, 1e-9)`
- **现象**：阈值是中位数的一半，任意启发式，与业务量级无关；median=0 时变成 1e-9，几乎所有非 0 值都被标记为异常。
- **风险**：异常报告的敏感性极不稳定，依赖数据 scale；median=0 数据集会出现"全异常"误报。

---

### P3-15. `_comparison` 强制恰好两行

- **位置**：[app/analysis/engine.py](file:///root/yyy/DataAnalysis_Agent/app/analysis/engine.py) `_comparison()`：`if len(values) != 2: raise AnalysisError("对比结果必须明确返回且仅返回基期与对比期两行")`
- **现象**：意图枚举叫 `COMPARISON_ANALYSIS` 但实际只支持 2 组对比；多组对比会被误判为失败。
- **风险**：业务上"对比 N 个对象 vs 基期"无法完成；用户得到错误"必须且仅返回两行"但意图本就是多对象对比。

---

### P3-16. `_root_cause` 用列名关键词识别贡献列

- **位置**：[app/analysis/engine.py](file:///root/yyy/DataAnalysis_Agent/app/analysis/engine.py) `_root_cause()`：`contribution_columns = [c for c in columns if any(signal in c.lower() for signal in ("贡献","变化","差额","影响","增量","delta"))]`
- **现象**：命名约定弱，依赖 SQL 端把列名命成这些关键词；不命中则直接 raise。
- **风险**：归因能力在跨语义模型时极易失效；不同业务域对贡献列命名千差万别。

---

### P3-17. `HttpSemanticAdapter.resolve_metrics` 返回空列表而非报错

- **位置**：[app/adapters/http.py](file:///root/yyy/DataAnalysis_Agent/app/adapters/http.py) `resolve_metrics()`：`if data.get("status") != "RESOLVED" ...: return []`
- **现象**：上游 status=FAILED 也变成空 list；orchestrator 后续 `_metadata_answer` 看到 `len(resolved) != 1` 走 fallback "没有找到唯一、已发布的指标定义。"
- **风险**：上游业务错误（如语义模型未授权、租户无权限）被伪装成"指标未找到"，排障路径错误；监控告警分类错误。

---

### P3-18. `HttpDataRetrievalAdapter.query` 用同一 `request_id` 作幂等键调两个上游

- **位置**：[app/adapters/http.py](file:///root/yyy/DataAnalysis_Agent/app/adapters/http.py) `query()`：先调 asl_generator，再调 sql_translator，两次都用 `idempotency_key=str(request.request_id)`
- **现象**：上游若按幂等键去重，第二次调用会被当作 asl_generator 的重放，返回错误结果或被拒。
- **风险**：依赖上游实现幂等键作用域隔离；契约未规定，跨服务幂等行为不可预期。

---

### P3-19. `knowledge_base_search_path` 同时承担 verify 与 retrieve-analysis

- **位置**：[app/adapters/http.py](file:///root/yyy/DataAnalysis_Agent/app/adapters/http.py) `verify_metric()` 与 `retrieve_analysis_context()` 都 POST 到 `knowledge_base_search_path`，但 payload 完全不同
- **现象**：verify_metric 发 `query="指标 X 口径 版本 Y"`；retrieve_analysis_context 发构造的分析类型描述。契约 `VerifyMetricRequest` 与本 payload 完全无关。
- **风险**：契约设计与运行时行为偏离；上游若按 payload 严格校验，verify_metric 会被拒；上游 schema 变更极易同时破坏两条链路。

---

## P4 — 风格 / 边界 / 微缺陷

### P4-1. `_number` 对 Decimal 走 `float(value)` 路径损失精度

- **位置**：[app/analysis/engine.py](file:///root/yyy/DataAnalysis_Agent/app/analysis/engine.py) `_number()`：`if isinstance(value, (int, float, Decimal)): number = float(value)`
- **现象**：`math.isfinite(number)` 仅对 float 有意义；Decimal 走该分支时 `float(value)` 可能丢失精度。
- **风险**：高精度金额计算精度损失；分位/累计值与原始 Decimal 表征出现轻微偏差。

---

### P4-2. `_temporal_labels` 用 `(year, month, day)` 三元组，未覆盖周粒度

- **位置**：[app/analysis/engine.py](file:///root/yyy/DataAnalysis_Agent/app/analysis/engine.py) `_time_key()` 与 `_temporal_labels()`
- **现象**：ISO 周（`2024-W01`）、半月、季度末工作日等粒度无法解析；`sortable != sorted(sortable)` 时直接 raise。
- **风险**：周粒度趋势/预测直接 raise "无法验证顺序"；季度末业务报表在跨年边界处易出错。

---

### P4-3. `HttpDataRetrievalAdapter._dataset` 用 `request_id + rows` 做 fingerprint

- **位置**：[app/adapters/http.py](file:///root/yyy/DataAnalysis_Agent/app/adapters/http.py) `_dataset()`：`fingerprint = sha256((request_id + json.dumps(rows)).encode()).hexdigest()[:20]`
- **现象**：同一请求重试（不同 request_id）得到不同 fingerprint；不同请求的相同数据集无法去重。
- **风险**：fingerprint 语义是"哪次请求"而非"哪份数据"，审计追溯易混淆；下游缓存命中失效。

---

### P4-4. `orchestrator._request_fingerprint` 把 `identity.roles` 纳入 hash

- **位置**：[app/services/orchestrator.py](file:///root/yyy/DataAnalysis_Agent/app/services/orchestrator.py) `_request_fingerprint()`
- **现象**：`roles` 来自 `identity` 而非 chat body；网关注入 role 略有变化（如新增 admin role）会让相同问题变成"不同请求"。
- **风险**：客户端重试时若网关 roles 变化会被判为新请求；幂等性受限；同租户同用户同问题的缓存命中率下降。

---

### P4-5. `memory_http_error(exc)` 把所有非 MemoryError 异常映射为 503

- **位置**：[app/api.py](file:///root/yyy/DataAnalysis_Agent/app/api.py) `memory_http_error()`
- **现象**：包括 `pydantic.ValidationError`、`KeyError`、`AttributeError` 等内部 bug 也返回 503。
- **风险**：客户端看到 503 但实际是 500；监控告警误判上游故障；后续若加新 MemoryError 子类需要手工扩展。

---

### P4-6. `main.py custom_openapi` 硬编码路径名

- **位置**：[app/main.py](file:///root/yyy/DataAnalysis_Agent/app/main.py) `custom_openapi()`：硬编码 `/v1/data-analysis/chat`、`/v1/data-analysis/chat/stream`
- **现象**：路径若变更，`paths` 字典会失效且不报错。
- **风险**：OpenAPI 文档的 `required` 字段标记与实际路径漂移；前端按文档生成的 SDK 误用必填参数。

---

### P4-7. `_understood_text` 用 `end_exclusive - timedelta(days=1)` 表示"含首尾"

- **位置**：[app/services/orchestrator.py](file:///root/yyy/DataAnalysis_Agent/app/services/orchestrator.py) `_understood_text()`
- **现象**：假设 end_exclusive 是天粒度；月/小时粒度时"含首尾"展示与实际语义不一致。
- **风险**：用户看到"时间范围=2024-01-01 至 2024-01-31"实际可能是到 2024-02-01 00:00:00；非天粒度时口径误导。

---

### P4-8. `stores.session.RedisSessionStore.from_url` 签名未在本审查中完整核验

- **位置**：[app/stores/session.py](file:///root/yyy/DataAnalysis_Agent/app/stores/session.py)（仅读了前 120 行）
- **现象**：`dependencies.py` 调用 `RedisSessionStore.from_url(redis_url, ttl_seconds=..., response_ttl_seconds=..., prefix=...)`；本次审查未完整读取该类实现，无法确认 4 个 kwarg 全部存在。
- **风险**：若 kwarg 名或数量不一致，启动时立即 `TypeError`；建议补完整读取并写契约测试。

---

### P4-9. `AnalysisEngine._labels` 当只有 metric 列时 label 全为序号

- **位置**：[app/analysis/engine.py](file:///root/yyy/DataAnalysis_Agent/app/analysis/engine.py) `_labels()`：`label_column = next((column for column in columns if column != metric_column), None)`
- **现象**：单列结果集走 `[str(row.get(None, index + 1))` → `row.get(None)` 必返回 None → 全部 label 是 `str(index+1)`。
- **风险**：单列结果的标签变成 1/2/3 数字，用户无业务语义；占比/异常报告的"标签"对用户无意义。

---

### P4-10. `_composition` 拒绝负值，`_comparison`/`_trend` 不拒绝，行为口径不一致

- **位置**：[app/analysis/engine.py](file:///root/yyy/DataAnalysis_Agent/app/analysis/engine.py) `_composition()` vs. `_comparison()` / `_trend()`
- **现象**：占比场景拒绝负值（合理），但对比/趋势允许负值（如利润率）；文档未明确口径。
- **风险**：业务上"负占比"是否合法？需文档化口径；负值场景的 `pct_change` 已正确处理 0 基期。

---

## 附：审查覆盖范围

已读文件（按层）：

| 层 | 文件 | 行数 |
|----|------|------|
| 入口 | [app/main.py](file:///root/yyy/DataAnalysis_Agent/app/main.py) | 98 |
| 入口 | [app/api.py](file:///root/yyy/DataAnalysis_Agent/app/api.py) | 363 |
| 入口 | [app/dependencies.py](file:///root/yyy/DataAnalysis_Agent/app/dependencies.py) | 89 |
| 入口 | [app/config.py](file:///root/yyy/DataAnalysis_Agent/app/config.py) | 117 |
| 编排 | [app/services/orchestrator.py](file:///root/yyy/DataAnalysis_Agent/app/services/orchestrator.py) | 805 |
| 工作流 | [app/graph/workflow.py](file:///root/yyy/DataAnalysis_Agent/app/graph/workflow.py) | 27 |
| 适配器 | [app/adapters/base.py](file:///root/yyy/DataAnalysis_Agent/app/adapters/base.py) | 64 |
| 适配器 | [app/adapters/contracts.py](file:///root/yyy/DataAnalysis_Agent/app/adapters/contracts.py) | 329 |
| 适配器 | [app/adapters/errors.py](file:///root/yyy/DataAnalysis_Agent/app/adapters/errors.py) | 115 |
| 适配器 | [app/adapters/handshake.py](file:///root/yyy/DataAnalysis_Agent/app/adapters/handshake.py) | 80 |
| 适配器 | [app/adapters/http.py](file:///root/yyy/DataAnalysis_Agent/app/adapters/http.py) | 427 |
| 适配器 | [app/adapters/mock.py](file:///root/yyy/DataAnalysis_Agent/app/adapters/mock.py) | 62 |
| 分析 | [app/analysis/engine.py](file:///root/yyy/DataAnalysis_Agent/app/analysis/engine.py) | 380 |
| 存储 | [app/stores/__init__.py](file:///root/yyy/DataAnalysis_Agent/app/stores/__init__.py) | 23 |
| 存储 | [app/stores/session.py](file:///root/yyy/DataAnalysis_Agent/app/stores/session.py) | 326（仅读 120） |
| 存储 | [app/stores/redis.py](file:///root/yyy/DataAnalysis_Agent/app/stores/redis.py) | 75 |
| 存储 | [app/stores/long_memory.py](file:///root/yyy/DataAnalysis_Agent/app/stores/long_memory.py) | 100（仅读 100） |

未完整读取的文件（建议补审查）：
- `app/stores/session.py` 第 121-326 行（`RedisSessionStore.from_url` 实现，对应 P4-8）
- `app/stores/long_memory.py` 第 101 行以后（`MySQLLongTermMemoryStore` 实现）
- `app/intent/classifier.py`、`app/intent/structured.py`（意图识别层未深入审查）
- `app/domain/models.py`（领域模型，仅按引用片段理解）
- `app/tools/` 目录
- `tests/` 目录（仅 `test_handshake.py`、`test_redis_session.py` 出于引用追踪需要被检索过）

---

## 优先修复建议（顺序参考）

1. **P0-1 / P0-2 / P0-3 / P0-4**：Handshake 启用 + AdapterBundle 字段/Protocol 补齐 + Policy & Analysis 适配器实现。四项互相耦合，应一次设计、分步实施。
2. **P1-1**：API 入口 HTTPBearer。低成本、高收益、独立项。
3. **P2-1**：SAFE_FALLBACK 保留 evidence（约 2 行代码）。
4. **P2-2**：warnings 进 answer（约 3 行代码）。
5. **P2-3**：reliability 区分 critical/advisory warnings。
6. **P2-4 / P2-5**：删除 `errors.py` 与 `stores/redis.py` 孤儿文件，或反向合并 base 与 errors。
7. **P2-6**：SSE 幂等校验前置。
8. **P3 系列**：契约对齐、模型名修正、`/ready` 扩展、配置 guard 等，按业务节奏分批处理。

> 本文档仅记录问题与风险，未修改任何源代码。修复建议方向供后续单独排期实施。
