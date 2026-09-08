# 数据智能体 Git 工作约定

对数据智能体做任何功能性修改后，必须完成以下收尾工作：

- 运行与改动范围相匹配的测试或接口验证，并记录结果。
- 自动创建 Git 提交。提交标题应概括功能变化，提交正文应说明主要改动和测试结果。
- 提交前不得纳入 `.env`、日志、缓存、备份或任何凭据。
- 向用户报告提交哈希、改动摘要和验证结果。
- 默认只提交到本地仓库；除非用户明确授权并已配置远程，否则不得执行 `git push`。

## 长期工程工作合同

依据用户 2026-09-08 的正式要求：对明确的当前目标，自主完成调用链审计、方案及自我审查、最小修复、专项测试、Critical Suite、全量回归、Code Review、明确清单同步、提交、功能分支 Push 和 Draft PR。上述步骤已获授权，不逐步请求确认，不自动合并。没有明确目标时不自行启动无边界重构。

- 修改前确认开发目录、Git 状态、分支、提交、阶段、PR 和现有测试基线。开发在 `E:/YouoAgent/DataAnalysis_Agent`、`E:/YouoAgent/Oagnet` 或 `E:/YouoAgent/sql-translator`；版本仓为 `E:/yy`，远程为 `dreamyang-1/data`。
- 沿真实调用链定位 first divergence。结论标注 `PROVEN`、`HIGH_CONFIDENCE` 或 `UNKNOWN`，不能把测试期待或旧总结当成业务事实。
- 业务后端是认证、授权和角色权限的唯一权威。Agent 只执行当前请求的 Semantic Scope，不实现 RBAC、SSO、OAuth 或角色到业务域映射。
- `semantic_model_id` 必传且为严格正整数。空业务域表示当前模型下 `MODEL_WIDE`；单一显式域只允许该域。当前显式多域必须 Fail Closed，不能变成 model-wide，不能自动加入共享域 `-1`。
- History、Pending、Task Frame、Dataset、Memory 和 LLM 都不能创建或扩大授权。恢复和缓存复用必须核对当前 Scope。
- 业务后端保证 `conversation_id` 全局唯一，它是可信会话主隔离键。tenant/user 是兼容状态元数据，不是数据权限。没有稳定 user principal 时，不得以 `default-user` 提供跨 conversation 的个人长期记忆。
- 已通过 Critical Suite 的 V1 多轮行为冻结，除非有可稳定复现的新 P0。Turn Referential Completeness 与 Execution Readiness 分开；Pending 不劫持新任务；状态操作由确定性 Reducer 执行。
- V2 保持既有 Shadow 边界。没有评测证据和相应授权，不做生产切流。
- 后续输入输出格式以现有代码为准，不自行设计新的请求字段、响应结构或 SSE 格式。达到能够替代 V1 的验收条件后，先与用户确认，再切换。
- 根因属于 Oagnet 或 SQL Translator 时修改对应服务；不能在 DataAnalysis 加错误补偿。每个服务分别记录文件、根因、测试及提交。未知或不支持的 Scope/计划继续 Fail Closed。
- 优先修 Contract、Schema、State、Reducer 和 Grounding。Prompt 或 Regex 变更必须有根因、正反例和回归证据，不能通过猜测语义减少追问。
- 失败先分类；仅凭正式合同才能修改旧断言，并记录 `STALE_TEST`。记录 baseline/final、collection errors、old-pass → new-fail、old-fail → new-pass 和新增测试。
- 默认只运行离线测试及 Mock；禁止未经明确授权的生产写入、索引重建、权限修改、数据库不可逆变更和生产切流。
- 按明确清单逐文件同步并比较 SHA-256；禁止整目录覆盖、`git add .`、`git add -A`、`reset --hard`、`clean`、stash 用户改动和 force push。不得提交凭据、内部敏感地址、环境文件、生产结果、日志、缓存或备份。
- 正式边界变更同步检查文档。核心证据以 closure report、test delta、change manifest、known blockers 为主，避免重复文档。
- 只有当前目标的 P0 清零、Critical Suite 通过、无新增回归和收集错误、证据及 Review 完整、Git 干净时才宣布当前阶段完成。保留真实阻塞，不宣称生产就绪。

## 当前唯一目标：V2 Production Replacement Readiness

依据用户 2026-09-08 的新要求，从已完成的地区列表修复（PR #17）之后：

- 停止无边界新建 Phase0C 子阶段修普通 Legacy 问题。每项问题先证明是否阻塞 V2 Cutover；非阻塞失败进入 Backlog，不为数字清零改代码或旧断言。
- 使用 `docs/cutover/` 维护 Cutover Readiness Matrix、剩余失败分类、目录版本/发布证据和最短阻塞路径。UNKNOWN 必须调查；缺少权威标签或运行证据不能被当成 PASS。
- 优先完成 MySQL → Catalog snapshot/release → Oagnet/Milvus → 查询/缓存/绑定版本身份和漂移检测。纯函数、离线导出比对、旧本地快照均不能替代真实发布证据。
- Catalog/Scope/关键状态安全门禁通过并正式结束 Phase0C 后，进入 Phase0C-2：80～120 条高质量 Gold、确定性 V1/V2 分项评估，再旁路比较正式可用模型及 Thinking Mode。保持生产默认模型。
- Evaluation 门禁通过后才开展 Phase1A plan-only shadow；V2 不执行真实 SQL、不写生产业务状态、不影响 V1 回答。真实 Shadow 通过后才准备不超过 5% 的受控 Canary、自动停流和 Rollback。
- 每次阶段汇报必须包含 Current Stage、Cutover Blocker P0/P1、Catalog/Evaluation/Shadow Gap、V1 Replacement Readiness 和 Next shortest blocking path。
- 只有全部替代条件满足时报告 `READY_FOR_USER_APPROVAL` 并停止自动推进，明确问“是否批准V2正式替代V1？”。在用户明确批准之前，不关闭 V1、不让 V2 全量接管、不删除 Legacy fallback。
