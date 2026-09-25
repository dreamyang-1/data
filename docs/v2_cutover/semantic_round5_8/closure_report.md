# Round 5.8 — Recovery Baseline & Historical Reference Closure

**V2_RECOVERY_AND_EXECUTION_PREPARATION_PARTIAL**

恢复影响核查、历史引用的最小生成说明修改及限定验证已完成。10 个新 Live 轮次均通过已声明语义轴；目标计划的原生 typed lowering 实际返回 `ASL2_TIME_STORAGE_TIMEZONE_UNPROVEN`，因此没有扩展执行 Adapter。C2 地区粒度仍缺业务依据。没有切流、SQL/E2E 或下一阶段工作。

唯一机器证据索引：[evidence_index.json](evidence_index.json)。其中包含模型角色与恢复影响矩阵、逐轮判断、实际回执 hash、原始失败、版本、调用预算、测试差异和明确 changed-file manifest；敏感配置及原始 capture 只留 PRIVATE。

## 版本与运行基线

开发 `E:/YouoAgent/DataAnalysis_Agent`；Git `E:/yy`。Baseline 为 `7ab840df09c4b1e57bf0acf2bf180914a7db7c2c`，父 [PR #66](https://github.com/dreamyang-1/data/pull/66) 开始时 OPEN/DRAFT/未合并。1673 个 tracked file 开发/版本仓/上轮证据一致，Git 干净，无并行源码修改。分支 `v2-recovery-typed-execution-prep-20260910t113903z`。最终提交通过 `git log --diff-filter=A -1 --format=%H -- docs/v2_cutover/semantic_round5_8/evidence_index.json` 解析，发布回执由索引定位，不在提交内部递归写自身 SHA。

开始 2026-09-10 11:39:03 UTC；目标 13:09:03，硬停止 13:39:03。正式源码修改仅 `context_contract.py` 的通用 description/version；另同步一份已有生成 Schema，新增专项测试与两个受限评测工具。V1 代码/API/SSE/UI 未修改。

恢复进程均在 19:26:39 +08 启动：Agent PID 26572 /8088，Oagnet PID 27968 /8021，SQL Translator PID 26172 /48000。已核对启动入口、WorkingDirectory 启动记录、PID/创建时间与无 `--reload`；保持原进程，没有为了开发停服务。三服务仍运行 baseline 代码；新 V2 description 只用于本轮隔离验证，未部署为 V1 正式入口。收尾原生 `/live`、Oagnet `/`、SQL `/api/health` 均 HTTP200；最初误用 Agent 不存在的 `/health` 所得 HTTPError 另行保留，未当作服务故障或 E2E 结果。

## 恢复操作核查

已读取实际 diagnosis/repair/verify 脚本及回执。恢复真正改变的是 workspace `.env` 的 `API_KEY/DASHSCOPE_API_KEY/LLM_API_KEY/LLM_MODEL_ID`，以及 `New_Agent/.env:API_KEY`。**Agent 自身 `.env` 与 Round5.7 一致**；当前三份配置与本轮开始逐一相同，仅输出相同/不同，没有公开密钥或密钥 hash。

| 角色 / 实际调用方 | 当前服务模型与参数 | 来源、加载与证据边界 |
|---|---|---|
| V2 CurrentTurn / SemanticEdits，RecognitionModelClient | qwen3.7-max；T=0、Thinking=false，评测 retry=0/timeout=60s | Agent Settings 在 Oagnet fixture import 前快照；上轮24、本轮20请求均HTTP200；本轮新 description 已进入实际 Schema |
| V1 StructuredIntent | qwen3.7-max；T=0、Thinking=false、retry=1/30s | Agent 显式配置及启动时构造 Settings；没有本轮逐角色 Live |
| Analysis synthesis | qwen3.7-max；T=0、Thinking=false、retry=0/8s | 同一 Agent key；角色模型配置保留 |
| Chat responder | qwen3.7-max；**T=0.5**、Thinking=false、retry=0/8s | 保留实际对话参数，没有强制统一温度 |
| Task DAG / Tool selector | qwen3.7-max；T=0、Thinking=false；DAG 15s，Tool 30s/retry=1 | 使用既有 intent Settings；没有额外调用 |
| Oagnet ASL | qwen3.6-plus → qwen3.7-max；T=0、Thinking=false、retry=0/45s | workspace key 随恢复改变；新进程及恢复后 `/agent/query` HTTP200/16.525s 证明该路径可用 |
| Embedding | text-embedding-v4 /1024；retry=2/60s | 只换 key，专用模型/endpoint/维度配置未变；恢复时专用 smoke 成功，未重建索引 |
| SQL Translator | 非模型消费者 | 没有增加 LLM 配置 |
| 其他 New_Agent 旧入口 | 后端传模型，代码仍有 qwen3-max fallback | `.env` 换 key 不等于 `config.py` 中静态 key 已同步；无本轮该服务进程/调用。`CURRENT_CONFIGURATION_UNVERIFIED`，不在当前 Agent→Oagnet→SQL 路径，未擅自再配置 |

当前路径均为 DashScope OpenAI-compatible endpoint。Oagnet 的 fallback `qwen3.6-plus` 仍存在于代码，但当前 root 配置选中 qwen3.7-max。Agent Settings 使用缓存；Oagnet config 在 import 读取，Embedding client 延迟缓存，不能凭 Git 干净推断配置不变。业务授权 `semantic_model_id=81 / domains=[205]` 与 LLM 服务模型是不同合同。

密钥本地比较一致，只证明配置引用一致；新 key 绑定的网关项目、Provider 内部 revision/路由没有证明，保留 UNKNOWN。没有逐个读取进程内存或宣称所有未调用角色均已实测。

## EVIDENCE_IMPACT_MATRIX

| 证据 | 恢复影响与历史有效性 | 当前适用性 / 最小补测 |
|---|---|---|
| A 离线回归、冻结回放 | key 不影响无外网的源码合同；历史版本证据保留 | 本轮专项及三服务累计离线回归；生成 description 变化后不伪称旧请求严格 Replay |
| B Round5.7 V2 Live + 冻结目录/源值 | 实际走 Agent key，未调用在线 Oagnet ASL/Embedding；24次HTTP200保留 | 旧 A3/C2/C3 有合法输出，不能归因于 Oagnet401。本轮仅补新生成输入的10轮 |
| C 在线向量检索 | 旧401是可用性问题；恢复后专用Embedding可响应 | 未认证新 key 的 Provider revision 与既有索引构建空间；不能以维度相同宣称检索质量通过 |
| D 在线 Oagnet ASL | 模型与 key 发生变化，旧配置结果只作历史基线 | 复用恢复后16.525s成功回执，没有再发 ASL 请求，不等于SQL结果正确 |
| E 平台HTTP/SSE/最终执行 | 路由源码未变 | 健康检查与ASL回执不足以证明业务E2E；本轮未执行 |

Embedding 的 query/document 共用 `embedding.py`；向量本身没有新增归一化，Milvus 使用 COSINE。文本侧按原路径保留：普通语义检索使用 user_query，实体候选 API 使用 `normalize_catalog_text`，入库文档使用既有序列化及标点/空白规范化。相关源码与 baseline 一致。四个 collection 的恢复健康回执证明配置维度；发布合同可绑定 `embedding_contract/vector_index_version`，但回执没有给出实际部署集合的完整 Provider 构建 revision。结论是**没有已知本地模型/预处理改变，不重建；完整实时空间兼容性未认证**。Round5.7 冻结源值证据不用于抵销这一缺口。

## A3、C2、C3

**A3：评测层级混淆，非 CLEAR 生产失败。** `context_proposal.validate_proposal` 明确只验证关系/合法目标，delta/barrier 留给 TaskPatch/Reducer；`proposal_resolution` 的 MODIFY 与 CLEAR 都引用当前任务，独立 `TaskPatch.clears` 决定操作。原生旧 resolver 对有操作的普通追问也可给 MODIFY。公共路由未使用 V2 关系标签执行条件清除。

因此原 A3 的 MODIFY + CURRENT_EXPLICIT CLEAR、空 Filter、下一轮 barrier 持续合法。新增 evaluator `healthy-slice-relation-operation-v2` 要求实际 CLEAR、非空证据、正确 base/target/version、Scope/Pin、不改变其他语义、完整 State/IR/Plan 及下一轮无复活；缺任何证据均失败。8类变异负例已通过，未用宽泛标签等价放宽验收。原严格 **8 PASS /3 FAIL** 及 capture 不动；同一记录新版本 **9 PASS /2 FAIL**，唯一改分 A3，不是模型改善。

**C2：REGION_GRAIN_EXPECTATION_GAP。** city.city_name 是 `dim_city.city_name`，地级市/城市档案；province.province_name 是 `dim_province.province_name`，省级行政档案。目录分别经 `hospital.city_id→dim_city.city_id`、`hospital.province_id→dim_province.province_id` 连接；共同上游为 `sales_order.hospital_id→hospital.hospital_id`。不存在“北京市在两字段统计必然相同”的正式规则。新任务也没有理由继承旧 province 字段。本轮不改旧标签、不换绑定、不执行源 SQL，不宣称 C2 整体正确。新明确“江苏省”控制请求不是 C2 的替代验收。

**C3：历史描述被错误声明为当前 subject。** 原始 Parse 生成该义务；Context 已选择正确历史 Task，没有规则覆盖模型；Draft 无 subject edit，既有守卫正确拒绝。离线只纠正 Parse 的引用声明，即可恢复原目标及正确语义。第二阶段记录只有 INHERIT 与仍合法的历史 handle，没有消费变化后的 mention/binding/source handle；仍明确其输入 context 已变，属于 Oracle，不能计 Live/严格 Replay。

最小修改为一处现有 Context Schema description，版本 `v2-context-reference-intent-v3`：只定位 Task 的描述使用引用信号，真实当前时间/指标/分组编辑继续声明并消费。没有新 Context 模型、Regex、业务词特判、confidence、Runtime slot 删除或 Guard 放松。独立 Parse/Draft Prompt 常量不变，**有效模型输入确有 description 变化**，已用新模型请求验证。

| 新 Live | 关系 / 结果 | 已核对语义 |
|---|---|---|
| Fresh 首问 | NEW_TASK / PASS | 江苏省、订单笔数、2025年 |
| 原 C3 纯历史返回 | RETURN_TO_TOPIC / PASS | 原 Task A、完整语义不变、其他任务未改 |
| 历史返回 + 改今年 | RETURN_TO_TOPIC / PASS | 只改时间到2026年，保留地区与指标 |
| 历史返回 + 加销售总数量 | RETURN_TO_TOPIC / PASS | 保留订单笔数并新增指标 |
| 历史返回 + 按城市分组 | RETURN_TO_TOPIC / PASS | 目录允许 order_count/city；保留省份过滤，新增city维度；不认证SQL可执行 |
| 明确独立省级新查询 | NEW_TASK / PASS | 当前数量指标/时间/省级值，无旧任务修改 |
| 普通时间追问 | MODIFY / PASS | 原指标、地区保留，只改时间 |
| 不限地区 → 换今年 | MODIFY / 两轮PASS | 实际 CLEAR，下一轮地区不复活 |
| 不要订单笔数 | REMOVE / PASS | 从原真实双指标状态只留下销售总数量 |

10轮/20请求，0 FAIL/NOT_RUN/BLOCKED，均为**已声明语义轴**。C3四个分支恢复原真实前置，REMOVE恢复旧B链真实前置；Fresh及其四个后续分支为本轮执行，不能叫作原A/B/C三链全量重跑。其他任务未改、输入状态未原地变异、模型合法提案直接被接受；本次10个关系决策 hard/soft veto 均0，这不是对抗安全覆盖率。

残留明确保留：四条历史请求仍输出非显式角色假设，而非完全没有 task-description mention；它们没有声明当前 subject slot/marker，也未产生额外编辑。不能宣称模型已完全遵守“省去所有引用描述 mention”的说明。新增反例确认即使 `explicit=false`，只要仍声明 slot/marker，遗漏消费继续拒绝；并非用该标志免除用户明确要求。目标不存在、Scope/Pin不兼容、Pending、真歧义沿用本轮新跑的现有负向合同测试。

## 实际 typed lowering 与执行准备

前20分钟内在断网/冻结源值条件下，恢复3份原已接受计划：A首问、B首问、B最后一轮。Scope/Pin/plan身份检查通过，真实调用 `asl2.lower_asl2`，全部 **UNSUPPORTED / ASL2_TIME_STORAGE_TIMEZONE_UNPROVEN**；没有 ASL、SQL planning callback 或执行 transport。

目录已证明订单笔数为 `COUNT(DISTINCT sales_order.order_key)`，数量为 `SUM(sales_order.quantity)`；指标时间锚为 `sales_order.created_date`，当前计划有半开年度区间及 province Attribute Filter。物理 field24400/table1880 为 DATETIME，没有存储时区声明。查询 TimeSpec 的 Asia/Shanghai 不能补成存储证据。当前 lowerer 对有界时间显式拒绝，且尚无该时间 lowering；不能只移除守卫或删掉时间条件。

已沿 `compile_asl2`、`compile_executable_plan`、ResultContract/ExecutionAttemptRecord 检查边界。没有输出就不能声称公式、DISTINCT、时区、过滤树、join key、排序/limit 转换等价。关系目录可达也不等于 lowerer 支持目标路径。选取目标没有使用含义未确认的 C2。

按照“需要猜业务含义则停止扩展”的条件，**Adapter 未实现；ADAPTER_CONTRACT_PASS_TEST_ONLY 不成立**。既有测试重新证明 unsupported 不得提交SQL，Scope/Pin及结果合同错误不能接受；请求失败/错误结果不发布成功、Dataset保持、重试不重复应用等**新 Adapter 路径未到达，不记PASS**。没有用假结果、Plan hash 或 V1 Dataset 制造执行回执。

下一阶段该路径申请隔离只读 E2E 前，仍需存储时区/时间转换合同、余下关系和过滤 lowering 等价、注入 transport 与结果/状态安全合同。现在不具备此申请前提。Question completion 与公共响应接入继续保持上轮10项审计边界，没有把自然语言交回 Oagnet 重新规划冒充无损适配。

## 回归、性能与收尾

| 验证 | 结果与证据边界 |
|---|---|
| Agent一次完整离线分批运行 | 3457 passed /28 failed /0 collection errors |
| 当前生成文件同步 + 最终新增专项 | 18/18通过；其中17项新增合同测试，1项原 Schema 完整相等检查 |
| Agent最终已观察集合 | **3461 passed /27既有failed**；由上述回执按最终nodeid合成，未声称重跑第二遍全量 |
| 原681项受影响集合 | 全部保留通过；Critical160/160、Context8条18轮均本轮新跑 |
| Oagnet完整离线 | **690 passed /8既有failed /0 collection errors** |
| SQL Translator完整离线 | **381 passed /0 failed /0 collection errors** |
| old-pass → new-fail | **0**；旧单元测试 expectation 修改0 |

全量发现的第28项不是本轮 description 回归：在 baseline `7ab840d` 原样独立运行同样失败；`116ffe0` 已加入 SourceValueRequest selector 的 oneOf，但当前导出 `semantic_task_draft_v7.schema.json` 未同步。本轮仅补这一既有约束，其他 definitions 逐一一致；原完整相等断言不变，冻结0.2.1及实际生成逻辑不动，不影响本轮 Live 输入。已有27/8项失败保留，不为数字清零修业务。

测试开发第一版有新 fixture 自引用导致9个 setup error，已修测试闭包，无生产改动；完整回执保留。验证脚本中纠正了空slot列表比较、LogicalPlan无顶层query_shape等辅助检查错误，未改实际输出或业务期待。

模型请求20、HTTP200=20、401/403/timeout/retry=0，Embedding新调用0。输入282670/输出7900/总290570 tokens；10轮纯V2语义耗时10.594–27.406s，小样本观测p95=27.406s，各模型阶段独立计时见索引。恢复ASL16.525s属于不同时间/路径，不能直接比较；Embedding与local lowering未记录独立计时，真实E2E未测，无性能提速声明。

最终模型输入之后没有再改影响运行的源码。生成快照/测试/文档同步与 Live 实际请求 Schema 分开记录。三份生产配置不变；SQL执行、源SQL、生产状态/Catalog写入、索引重建、Blind访问全部0。测试中的 Fake/MemStore 写入不属于真实执行证据。无模型 Benchmark、Shadow、Canary、V1替换。

## 门禁、回滚与停止点

| 门禁 | 状态 |
|---|---|
| CONTEXT_FOLLOWUP_READY | NOT_READY_FULL_GATE；本轮限定关系/编辑轴通过 |
| INTERNAL_DEMO_SEMANTIC_READY | NO；原C2口径及其他既有整体门禁未关闭 |
| READY_FOR_INTERNAL_DEMO_E2E_SMOKE | NO；目标lowering拒绝，Adapter未接入 |
| QUESTION_COMPLETION_CONTRACT_GAP | OPEN_UNCHANGED |
| READY_FOR_USER_APPROVAL | NO |

本轮以 PARTIAL 收尾，不扩展下一 Root。最高执行阻塞是受治理存储时区与有界时间 lowering；语义侧保留C2口径决策与历史非显式角色假设等证据边界。没有恢复已关闭的初始化研究，也未自动启动真实SQL/E2E。

回滚以本轮独立提交为单位审查 `git revert <本轮commit>`；生产运行进程未切换。仅按明确manifest回滚源码/生成文件，不操作 `.env`、New_Agent配置或恢复期 PRIVATE备份。**源码回滚不得恢复旧失效密钥**。分支推送与堆叠 Draft PR 完成后停止，不自动合并。
