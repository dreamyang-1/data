# Round 5.9 — Time Storage Contract & Typed Execution Vertical Slice

**TIME_LOWERING_AND_EXECUTION_PREP_COMPLETE**

本轮关闭的是限定标量路径的时间编译与隔离执行准备：三份原年度计划、一个独立无时间计划均完成原生 lowering、原生 SQL 规划及 TEST_ONLY 执行/结果/状态验证。没有执行业务 SQL，没有把合成结果当作生产结果，没有接入公共平台或启动下一阶段。真实字段采用用户本轮明确声明的“北京时间”；历史迁移一致性仍未独立证明。

唯一机器证据索引：[evidence_index.json](evidence_index.json)。索引包含版本、明确文件清单、逐计划回执 hash、时间声明、原始拒绝、测试差异与回滚说明。原始 capture、测试驱动输出和环境核对只保存在 PRIVATE。

## 基线与运行保护

开发目录 `E:/YouoAgent/DataAnalysis_Agent`，版本仓 `E:/yy`。基线为 `20cb9d50fda03c8594778e766c23a6e4991915a5` / [Draft PR #67](https://github.com/dreamyang-1/data/pull/67)，开始时远程核对 OPEN/DRAFT/未合并，1678 个 tracked file 无意外差异，Git 干净。独立分支 `v2-time-lowering-execution-prep-20260910t123352z`，堆叠到 #67 分支。

开始时间 2026-09-10 12:33:52 UTC，目标 14:03:52，硬截止 14:33:52。Agent / Oagnet / SQL Translator 原进程 PID 26572 / 27968 / 26172 无 reload，运行状态保留；没有重启或切 8088。恢复后的三份配置逐一相同，未输出或提交凭据及其 hash。

Agent 修改四个内部生产模块，SQL Translator 修改一个现有执行函数的 opt-in 前置条件；新增 Agent 两个测试文件、SQL 一个测试文件。没有改 Prompt、模型输入 Schema、Context/Grounding、API/SSE/UI、V1 路由或冻结目录。SQL Translator 开发目录没有独立 Git，其版本以 `E:/yy/sql-translator` 的基线和逐文件 hash 确定。Oagnet 原 Git HEAD/index/用户工作树单独核对并保留，未同步覆盖。

最终提交由 `git log --diff-filter=A -1 --format=%H -- docs/v2_cutover/semantic_round5_9/evidence_index.json` 精确定位；提交后发布回执另记最终 SHA、远程 head/base 和 Draft PR，避免在提交内部递归写自己的 SHA。

## 1. 时间事实与声明边界

| 字段身份 | 类型 | 已确认语义 | 写入/转换依据 | 适用范围与未知项 |
|---|---|---|---|---|
| model81 / domain205 / data source58 / table1880 / field24400：`sales_order.created_date` | DATETIME，目录注释“创建日期” | 用户回答“北京时间”，记录为 DECLARED / LOCAL_WALL_DATETIME / Asia/Shanghai | 未找到对应订单写入器、ETL 时区转换或历史迁移合同 | 声明针对该实际字段；候选编译证据逐一绑定目标年度、Scope/Pin/物理字段。历史行是否全部遵循、是否有迁移分界仍 UNKNOWN |

有限核查覆盖相关 Java 后端、语义治理脚本、Oagnet/SQL 源码及冻结物理目录。现有治理材料将交易日期和指标时间锚映射到该字段，Java Web 配置有 Asia/Shanghai 序列化，但两者都不能证明订单入库转换。没有把 DATETIME、服务器时区或现有查询行为作为存储证据；没有运行新的源 SQL。向用户集中提出一次实际字段问题，收到“北京时间”后建立独立声明版本；未重复索要负责人。

查询 TimeSpec 在原生合同内保存带时区的瞬时边界，冻结时可规范为 UTC；这与物理 DATETIME 中保存北京时间是不同层次。新编译只将边界转换一次为已声明存储区的日期时间字符串，不对整列添加时区函数，不依赖机器默认时区。

`TimeStorageContract` 是可信调用方显式注入的内部编译输入，不进入 ChatRequest 或模型 Schema。它绑定证据版本/来源、完整授权上下文、字段 canonical ID、物理 field/table/source ID、元数据 hash、精度和适用区间。调用方必须另行固定证据 digest。TEST_ONLY 输入必须显式 opt-in；不能注册为生产目录事实。新声明没有改写旧 snapshot/hash/Pin，也没有修改原 LogicalPlan：新编译输入将原语义计划与新证据一起建立独立 fingerprint。

“声明适用于本次目标字段/区间”不是“已经审计所有历史数据”。本轮没有将 DECLARED 升级成写入链 PROVEN，也没有证明销售额的其他时间口径。

## 2. 实际有界时间 lowering

原 `lower_asl2` 在有界时间上直接拒绝。现在在证据校验后实际构造 `>= start AND < end`，与原 Filter 树使用 AND 结合，经既有 `pinned-filter-tree-v1` 和 `PYMYSQL_PYFORMAT_V1` 生成参数化 SQL。ASL 继续为 2.0；SQL Planner 校验 Scope、Pin、数据源、Projection、过滤/排序合同及 SQL+参数共同指纹。

| 原计划 | 本轮新声明下原生 SQL 规划 | 保留内容 |
|---|---|---|
| HC55-A 第0轮：去年江苏省订单笔数 | PASS | COUNT(DISTINCT sales_order.order_key)，江苏省，`[2025-01-01, 2026-01-01)` |
| HC55-B 第0轮：独立首问 | PASS | 同一业务含义、独立原 Task/Conversation 身份 |
| HC55-B 第3轮：数量指标、换今年后的原已接受计划 | PASS | SUM(sales_order.quantity)，江苏省，`[2026-01-01, 2027-01-01)` |

实际 SQL 保留 `sales_order.hospital_id → hospital.hospital_id` 及 `hospital.province_id → dim_province.province_id` 两段目录 Join，地区是 `dim_province.province_name`，标量 LIMIT 1。没有把名称当 Join Identity，没有改变 COUNT DISTINCT 或数量求和口径，没有删时间条件来换取通过。SQL 原生关系/基数检查通过，未出现需要补偿的下一层 Join 缺口。

原生 SQL 报告的 `TIME_CONTEXT_APPLIED=NOT_APPLICABLE` 专指旧 ASL `time_context` 槽；本轮时间走的是独立、强制消费的 typed Filter 合同。索引保存实际时间谓词、参数、证据 digest 和 SQL，不能用那条旧槽诊断代替本轮时间证据。

**不提供新证据时，三份原计划仍按 `ASL2_TIME_STORAGE_TIMEZONE_UNPROVEN` 拒绝。** 新结果是带版本声明的编译验证，不是对旧模型请求的严格 Replay，不改旧 Gold 评分。

测试覆盖北京时间和 UTC 存储、2025/2026 年度边界、不同查询时区、边界附近微秒、DATETIME 精度不丢失、原 OR/AND 树保留，以及字段/来源/Scope/Pin/digest/适用期间不符时拒绝。只支持明确范围的自然日历 DATETIME 转换；DST 地区、财政历、2000 年以前/2100 年及以后规则、DATE/TIMESTAMP 存储、时间比较/分组等保留 UNSUPPORTED。现有通用 DateTime Filter 的未证实存储 guard 未放松。

## 3. 独立无时间控制

采用已有公开 G81-015 原句 **“查询含税销售总额”**，原真实计划的 `time=None`，Certified Full Plan 标签也明确无时间范围。不是从“去年江苏省订单笔数”删除条件。当前原生 lowering 生成 `SUM(sales_order.amount_with_tax)` 的单表标量 SQL，无时间谓词，实际规划通过。

该控制与三份年度计划分别保留原输入/计划、Catalog 版本、编译 identity 和用途。它没有解决销售额按什么时间字段分析的其他问题，也不替代原年度查询的验收。

## 4. 内部执行协议与 Adapter

真实边界为：已授权 LogicalPlan → 原生 ASL 2.0/参数化 SQL → 显式注入的 transport → `SQLTranslatorProd.execute_sql_on_data_source` 参数协议 → 结果合同 → 测试状态回执。

没有把计划转成自然语言交回 Oagnet/V1。公共 `/api/execute` 不接受完整 V2 Pin/私有政策/参数回执，且当前 scoped 分支会重新按在线目录翻译，因此未用于这条链。本轮复用底层已有参数执行函数，不新增公共 API。

`prepare_execution` 复用同一 `ScopedPlanSession` 的原生规划和 Pin 结束校验。内部 Adapter **仅支持 SCALAR_AGGREGATE**，无默认 HTTP/DB transport，只接受显式注入的操作及 InMemory 测试 Store。非标量、Dataset 后续计算和公共结果接入继续在范围外。

复审发现旧执行函数在只读快照建立失败后仍可继续查询。本轮增加 `require_consistent_snapshot=True` 内部选项：只读事务、快照或快照时钟不能确认时，在业务查询前返回 `READ_ONLY_SNAPSHOT_REQUIRED`。Adapter 固定使用严格选项；V1 旧调用默认保持原行为。直接缺口在 SQL Translator 修复，没有在 Agent 假造成功证据。

四个最终控制均实际调用原生执行函数，但 PyMySQL 连接/游标完全为 Fake，结果为合成值；所有外网和业务库访问被测试隔离层阻断。**4/4 是 PASS_TEST_ONLY，真实 SQL 执行=0。** Native executor 生成的 snapshot 字符串也属于合成驱动回执，其外层 provenance 明确 TEST_ONLY；不能脱离外层宣称数据库已执行。

生命周期分别观察：PLAN_VALIDATED、INPUT_COMPILED、SUBMISSION_ATTEMPTED、EXECUTION_SUBMITTED、RESULT_RETURNED、RESULT_VALIDATED、SUCCESS_RECEIPT_SAVED。只有匹配当前请求/编译/Scope/source 的 transport 回执明确确认提交后，才记录 EXECUTION_SUBMITTED。超时保留结果未知，不把尝试提交写成确定执行。

结果要求完整列绑定、每行字段一致、标量一行、数值指标类型、实际 row_count、无下载预览/截断、只读一致快照质量证据及既有 `prove_asl2_result` 通过。结果 digest 在成功状态 CAS 之前计算，序列化错误不得在已保存 Dataset 后才被发现。

状态复用已有 ConversationState、ExecutionAttemptRecord、DatasetState 和 ProofChain；不修改语义 TaskVersion、不重用 V1 Dataset。成功只更新目标任务的执行/Dataset 指针，其他任务和 active topic 保留。相同消息相同编译返回缓存回执；内容冲突拒绝；运行中重试不重复提交；timeout/失败不盲目重试。结果错误保留最近成功 Dataset。期间出现新 state version 时，旧请求不能覆盖新状态或保存成功；该请求保留失败回执，状态中原 RUNNING attempt 可待以后恢复处理。本轮没有实现持久化恢复系统。

本地请求/响应指纹证明可信注入边界中的对应关系，不是远程签名或生产执行证明。未产生可由公共 V2 Dataset restore 接收的生产 Result Artifact；原 plan-only 禁止 seal executed result 的 guard 保留。

## 5. 验证、复审与性能

| 集合 | 结果 | 边界 |
|---|---|---|
| Agent 最终已观察集合 | **3525 passed /27 既有 failed** | 候选v2一次完整25批回归3524/27，加最终v3仅 Adapter/其测试变化的专项回执；没有宣称v3再次完整重跑 |
| 最终受影响 Agent + SQL 专项 | **229 passed /0 failed** | 原生 lowering、Scope/Pin、参数执行、时间与新 Adapter 正反例 |
| 既有 Critical | **160/160** | 按上轮同一三个文件的 nodeid 集合比较 |
| Context Critical Slice | **8条/18轮通过** | 组件回归，未做新 Live |
| SQL Translator 完整离线 | **389 passed /0 failed** | 本轮新跑，新增8项严格快照测试 |
| Oagnet | **690 passed /8 既有 failed** | 复用 Round5.8；源码未变，不宣称新跑 |
| Agent 新增测试 | **64** | 24项时间、40项内部执行测试 |
| old-pass → new-fail / collection errors | **0 /0** | 既有业务 expectation 修改0 |

Round5.8 的一个未参数化测试在其最终提交前已变成四个参数化节点。重建基线时使用该提交的源码及最终回执，排除已被替代的中间观察，得到实际3461/27；不是删除当前失败或少算旧测试。本轮全部旧节点保留。

中间情况保留：最初命令重复 `--node` 只跑了旧 lowering 30项，未混算新增测试；新测试先后修正了冻结对象的 UTC 表示断言及尝试直接改 FrozenDict 的错误。私有验证脚本遇到 PowerShell 管道把中文转为问号，修正编码后重新检查原输出，未改业务期待。第一候选完整回归3523/27只作中间证据；随后复审补严格只读前置，重跑完整 Agent/SQL；最后仅修正提交 acknowledgment 的 trace 语义并验证全部受影响模块。完整回执和各版本均保留。

最后四个计划本地 lowering+原生 SQL 规划耗时约 **35.6–48.9ms**，Adapter+Fake 驱动约 **2.2–7.6ms**。样本4、Python3.12、当前本机隔离执行，不是生产数据库延迟/SLA，不与之前模型或在线 ASL 耗时混比。

新模型调用、Embedding、真实 SQL/源 SQL、生产 Redis/业务状态写入、Catalog 写入、索引重建、Blind 访问全部0。没有新增 Regex、语言关键词、业务词特判、Prompt 规则或常规模型调用。

## 6. 门禁与停止点

| 门禁 | 状态 |
|---|---|
| 真实字段存储语义 | USER_DECLARED_BEIJING；历史一致性 UNKNOWN |
| 有界时间 lowering | PASS_ON_DECLARED_CANDIDATE_AND_SYNTHETIC_CONTRACTS |
| 无证据/不兼容证据拒绝 | PASS |
| 标量执行 Adapter | PASS_TEST_ONLY |
| 独立无时间控制 | 原生编译及合成执行通过 |
| CONTEXT_FOLLOWUP_READY | NOT_READY_FULL_GATE |
| INTERNAL_DEMO_SEMANTIC_READY | NO |
| READY_FOR_INTERNAL_DEMO_E2E_SMOKE | NO |
| QUESTION_COMPLETION_CONTRACT_GAP | OPEN_UNCHANGED |
| READY_FOR_USER_APPROVAL | NO |

**当前不申请执行任何路径的真实 E2E。** 编译、参数协议、严格只读前置及结果/测试状态合同已具备本轮证据；当前生产 Pin/source 绑定、带明确超时预算的真实注入 transport 和隔离真实结果回执尚未验证。下一阶段应先处理这条最短执行依赖，再判断是否具备提出只读 E2E 申请的完整条件。无时间控制可独立评估，不能由它宣称原年度查询已在数据库验收。

历史迁移未知、C2 地区粒度、销售额时间口径、完整问题展示、其他 QueryShape/Dataset/DryPlan 和 Cutover 门禁继续保留。没有因为本轮编译通过而关闭这些门禁，也没有把它们重设为全部离线工作的全局阻塞。

## 回滚与发布

按本轮独立提交执行审查后的 `git revert <commit>`，以索引 changed-file manifest 为界。源码回滚包括 SQL opt-in 选项和内部 Adapter；不得回滚恢复后的有效密钥，不操作 `.env`、原服务进程或 Oagnet 原仓 index。仅显式同步并提交源码、测试和本目录两份文档。

功能分支 push、堆叠 Draft PR 与远程 head/base 核对后立即停止，不自动合并，不启动 SQL/E2E、公共接入、Benchmark、Shadow、Canary 或下一 Round。V1 继续正式服务。
