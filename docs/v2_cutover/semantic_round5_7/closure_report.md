# Round 5.7 — Fresh Query Vertical Closure & Platform Integration Audit

**FRESH_QUERY_VERTICAL_CLOSURE_PARTIAL**

原始首问“查询去年江苏省订单笔数”已从真正空会话建立正确的 TaskState、SemanticQueryIR 和 LogicalPlan：检查点1次、最终三条独立链各1次，共4/4通过已声明语义轴。最终11轮全部执行，10轮产生计划，1轮历史返回安全拒绝。严格检查保留 **8 PASS / 3 FAIL**：其中两项为关系标签或地区字段期待缺口，另一项为实际 Runtime 拒绝；没有将它们改成通过。三链门禁尚未全部通过，本轮按范围收尾，未开始下一 Root。

## 版本与边界

基线为 PR [#65](https://github.com/dreamyang-1/data/pull/65)，`a55cf33c3d4917548c7f4b47d01641199448ea52`，开始时 OPEN / DRAFT / 未合并。开发目录 `E:/YouoAgent/DataAnalysis_Agent`，版本目录 `E:/yy`；1669个 tracked file 与基线一致，工作树干净，没有未解释漂移。分支为 `semantic-fresh-query-closure-20260910t104402z`，堆叠到 Round5.6 分支。仅同步3个生产文件、1个已有工具、1个新增测试文件、本文及 results/rollback，共8项。

开始2026-09-10 10:44:02 UTC，截止12:44:02 UTC。固定 Scope81/[205]、as_of=2026-09-09T09:00:00+08:00、qwen3.7-max、Thinking=false、temperature=0、retry=0。模型配置、原fixture、Catalog及源值观察不变。生产候选在检查点前冻结，此后正式三链和最终测试对应同一源码 hash；没有测试后修改生产。实际提交可用 `git log --diff-filter=A -1 --format=%H -- docs/v2_cutover/semantic_round5_7/results.json` 解析；发布后远程 SHA/PR 以 `results.json` 指向的发布回执及 PR head/base 核验为准，不在提交内递归写自身 SHA。

本轮所有新模型输入/输出、完整 capture、测试回执、Oracle、源值证据均在 PRIVATE；正式 [results.json](results.json) 保存逐轮摘要、源码/输入/Schema/快照 hash、调用统计及证据索引。SQL、源数据库SQL、生产状态写入、E2E、Blind、Benchmark、Shadow、Canary均为0。V1、API/SSE/UI和8088保持不变。

## 原始失败与真实消费者

| 原始 Case | 用户筛选 / Parse | SourceValueRequest与候选 | 第二阶段编辑 | 最早不一致 |
|---|---|---|---|---|
| HC55-A | 江苏省；filter SET、metrics SET | 当前 FILTER_VALUE mention，目录 province_name handle；有合法精确源值观察 | 完整 Predicate 放入专用 FilterEdit ADD，引用同一 request_id | Draft把已声明 SET放入没有SET的专用ADD通道；Runtime报操作冲突 |
| HC55-B | 江苏省；filter ADD、metrics ADD | 同一筛选要求的请求存在 | 没有消费请求的Filter；metrics又写成SET数组 | Draft遗漏Filter；补上后才暴露后续metrics操作遗漏 |
| HC55-C | 同B | 同B | 同B | 同B；不因相同reason code宣称一个独立Bug |

第一阶段模型识别当前用户的筛选与操作；本题为空任务的初始化要求，并无“再加”旧条件。SET和ADD在当前证据下均可表达合法初始化意图，但在既有执行合同中属于不同通道，不能全局认为SET=ADD。第二阶段确实收到完整 Parse，用于补齐 binding、真实值和完整编辑；旧动态Schema没有联合约束槽位、声明操作和通道，因此允许再次选择不一致表达。没有发现正确模型输出被 Adapter改坏。

`source_value_recognition.resolve_requests` **已经存在**：先验证 `request_id` 与被 `value_request_id` 引用的集合、mention及合法字段，再执行受 Pin/Scope 约束的源值解析。`value_field_request_id`指向同一选择结果的字段。后续 `_patch` / `lower_edits` 编译 TaskPatch，Reducer建立状态，主链编译IR/Plan。检索回执不等于应用条件。

通用 `SlotEdit(filter_expression, SET)`携带完整原生 Predicate/Boolean tree；专用 `FilterEdit ADD`建立完整Predicate或针对已有目标修改成员。两者都能覆盖部分相同业务效果，但不能同时写入同一轮filter；Runtime原有 `V2_STRUCTURED_EDIT_CONFLICT`保留。初始化没有第三条编辑路径，也没有自动追加Filter、删除未消费请求、Top1、值字符串代替身份或city改名province。

## 因果验证与最小修改

修改前进行了8次离线诊断，包括3个原始控制。A仅将相同完整Predicate从专用ADD移入通用SET，完整原生链成功。B/C仅补完整Filter及原请求引用，下一步报 `V2_EXPLICIT_OPERATION_DROPPED`；同一Draft再对齐metrics ADD后，均建立正确指标、2025完整年度 created_date、江苏省省份绑定及一致状态/IR/Plan。修改点均在单个上游Artifact `SemanticTaskDraft`，B/C组合干预涉及该Artifact内两处；不是单字段实验。第一阶段原始输出不变，下游实际重算，无新模型调用。Oracle不计Live/Gold，也不作为新Schema的严格原请求Replay。

最终保留三项相互关联的修改：

1. 从既有正式 `filter_expression`值类型导出生成Schema，包括递归AND/OR/NOT、严格字段/源值handle。生成端禁止通用与专用通道同时写入；存在请求时要求有Filter结构。只在已解析为NEW_TASK且某槽位操作声明一致时约束对应metrics/dimensions/filter操作，保留混合操作及已有任务语义。没有运行时SET/ADD转换。
2. 更新3处现有生成Schema说明，明确整个Filter赋值、专用编辑及源值请求的引用/消费职责。没有修改Parse/Draft Prompt常量，但**有效模型输入Schema及description有变化**，不能声称Prompt输入完全不变。没有业务词、Case ID、地区词、Regex或confidence常数。
3. 新反例证明原生通用Filter SET可接受 `Predicate.scope=CURRENT_DATASET`，而专用ADD拒绝。提取现有的current-user/current-task验证并递归用于两条入口，统一拒绝。此为Predicate逻辑范围合同缺口，未观察到跨tenant/model越权，不能夸大为已发生数据泄露。

生成结构约束不替代请求精确消费、角色、字段、Scope、Pin及Reducer校验。第一阶段仍是模型证据；本轮根据本题明确原文核对其声明，没有证明任意Parse均正确。额外请求不会由Runtime自动升级为新筛选。原 singleton SET dict→list兜底未恢复，旧scalar SET拒绝反例原样通过。

## 最终Fresh逐轮验收

沿用Round5.6正式fixture，3条链各自空会话、Task/Pending/plans/history均为空，成功前缀复用0。检查点没有续成A，而是额外运行独立首问，共计2次检查点模型调用；正式链22次。没有因失败更换原文或反复采样。

| Chain / 轮 | 当前输入 | 模型/最终关系与计划 | 严格结果及状态证据 |
|---|---|---|---|
| A1 | 查询去年江苏省订单笔数 | NEW_TASK，Task A | PASS；order_count、2025 created_date、province_name=江苏省 |
| A2 | 换成北京市 | REPLACE，Task A | PASS；只替换地区，指标/时间保留 |
| A3 | 不限地区 | MODIFY，Task A | FAIL关系标签轴；实际专用REMOVE下降为CLEAR，filter为空且形成barrier |
| A4 | 换今年 | MODIFY，Task A | PASS；2026，filter仍为空 |
| B1 | 查询去年江苏省订单笔数 | NEW_TASK，Task A | PASS |
| B2 | 再加销售总数量 | ADD，Task A | PASS；原订单笔数与新数量同时存在 |
| B3 | 不要订单笔数 | REMOVE，Task A | PASS；仅余销售总数量 |
| B4 | 换今年 | MODIFY，Task A | PASS；2026，订单笔数未复活，江苏省保留 |
| C1 | 查询去年江苏省订单笔数 | NEW_TASK，Task A | PASS |
| C2 | 查询今年北京市销售总数量 | NEW_TASK，新Task B | FAIL字段期待轴；city.city_name=北京市，原Task A完整版本未改 |
| C3 | 返回刚才江苏订单笔数那个任务 | RETURN_TO_TOPIC，正确Task A；后续拒绝 | FAIL_SAFE_REJECT；`V2_EXPLICIT_SLOT_DROPPED` |

严格评分保留8/11。A3旧助手期待CLEAR/REMOVE关系，实际Context泛化关系为MODIFY，但实际TaskPatch明确CLEAR。单独验证清除、时间切换后防复活及其余语义轴均通过；没有修改原期待或计入额外严格PASS。

C2旧 `round54_plan_checks` 固定要求province_name；本次独立新任务选择目录city.city_name，并有该字段自身的精确源值身份、当前Scope和Pin回执。State/IR/Plan、指标和2026时间一致，Task A的完整versions/clear barriers保留。**不能据此认证city/province在本业务统计粒度上等价，也不能将city改名成province。** 地区粒度真值缺口独立保留，原严格结果不变。

C3的Context提案和原生验证已正确选择合法RECENT Task A，Scope/存在性/版本等约束通过，override=NONE。最早偏离在原始Parse：将“江苏订单笔数那个任务”整体声明为当前显式subject；Draft为INHERIT且无subject编辑，显式槽位保护拒绝。未返回新状态，输入状态与C2成功状态一致，原Task A没有污染。Patch/State/IR/Plan缺失仅是downstream effects。此为独立历史引用与当前subject义务问题，**不属于本轮首问初始化直接依赖**，登记后停止扩修。

已接受计划逐项比较状态/IR/Plan及字段与源值回执身份、版本和Scope。A2替换值时字段沿用旧mention，新的值回执引用当前mention；该来源区别有记录，不把mention不同误报成字段不同。12个实际执行轮（含检查点）的最终Context决策都接受模型合法提案，无hard/soft veto；重复内部校验日志不重复计数。该小样本并非对抗性或全面安全认证。

## 回归、性能与复审

受影响集合 **681 passed / 0 failed / 0 collection errors**，与Round5.6原650个nodeid逐一对照，新增31项，旧通过→新失败0、缺失0、旧测试源码/期待修改0。既有Critical集合160/160（Legacy回归49、Scope88、single-domain23）及Context Slice8条/18轮保持通过。旧Target15/15本轮重新执行，为明确版本化的Recorded signal migration；不算新模型准确率或旧请求严格Replay，Private仅机器重放、未逐条读取调参。

正反例覆盖完整Filter初始化、源值消费、已有ADD/REPLACE、Boolean结构、缺失/多余未消费请求、错误/重复引用、非法字段/handle、操作冲突和通道冲突；既有Source/Scope/Pin/目标版本/CLEAR/REMOVE测试复用。新wrong_scope反例初次失败暴露真实缺口，修改生产guard后原断言通过。诊断脚本自身曾修正`resolution_source`字段名及字段mention来源比较；均不修改旧capture或业务评分。

三服务全量均 **NOT_RUN_FRESH_CHAIN_GATE_FAILED**，遵守三条fresh全通过后才运行的条件。没有以历史全量结果冒充本轮，也未重跑相同全量。Oagnet/SQL生产源码未改。

模型调用24：检查点2、正式22、Smoke0；复用已核验相同配置的Round5.6成功Smoke，仅复用鉴权证据。HTTP200=24，重试/鉴权失败/超时=0。输入tokens346284，输出10868，总357152。正式11轮总观察耗时159.720s，各轮9.797–23.047s，小样本nearest-rank p95=23.047s；检查点18.063s。逐请求/模型阶段耗时和tokens见JSON。未独立测量原生阶段耗时，不补造数值；不宣称生产SLA、确定性或性能改善。正常路径仍每轮两次模型调用，无额外模型。

复审确认：没有运行时自动补条件；Schema约束来自既有值类型和当前操作信号；Scope、Pin、source consumer、Context及Reducer主体保持；新guard覆盖两条入口。候选源码在专项、检查点、最终三链和交付之间一致。工程验证通过不抵销历史返回及字段期待缺口。

## 平台执行链只读核查

以下为真实代码连接证据，不是部署HTTP/SQL运行验证；逐项函数及影响见JSON中的10项audit。

| 环节 / 文件与函数 | 连接状态 | 对E2E的影响 |
|---|---|---|
| `app/api.py` chat/chat_stream→invoke；`dependencies.py` build_container；`graph/workflow.py` skill_dispatch | 已接V1 DataAnalysisOrchestrator / HybridIntentClassifier | 平台请求没有选择RawTurnPlanner；Graph中的阶段名称不代表独立V2执行节点 |
| `semantic_v2/recognition.py` RawTurnPlanner.run | 真实内部入口；公共DI未接 | 需显式Scoped state/plans/pending及结果适配，改端口不能完成接入 |
| `catalog_bridge.py` compile / `_compile_logical`；`pipeline.py` compile_executable_plan | Raw主链已编译/验证计划；无执行提交 | ExecutablePlan描述不是SQL执行回执 |
| `catalog_bridge.py` compile_asl2；`asl2.py` lower_asl2；`harness_dryplan.py` | 原生独立seam存在，Raw/public未接 | lower支持范围有限；本轮没有调用它验证本题SQL。legacy_adapter只是能力评估 |
| `adapters/semantic_query.py` / `adapters/http.py` | V1已接Oagnet `/agent/query`、SQL `/api/translate` / `/api/execute`；V2未接 | 原适配器接CanonicalAnalysisRequest且Oagnet会做语义生成，不是V2 LogicalPlan无损执行器 |
| `result_contract.py`、`asl2.py` prove_asl2_result；`models.py` ExecutionAttemptRecord | 证明合同存在；V2执行消费链缺失 | SUCCEEDED要求plan/ASL/SQL/result证据，不能将Plan成功当执行成功 |
| Raw seal/return与`state_machine.py` DatasetState；Legacy `_persist_query_dataset` | V2返回封装状态，正式保存/执行Dataset桥接未接；V1有自己的保存路径 | 不能用V1 Dataset元数据或V2 Plan伪造executed receipt |
| `domain/models.py` ChatRequest / AgentResponse；`api.py` `_event` | 既有JSON/SSE合同保持 | 保留严格model ID、规范化domain、应用/会话/消息字段、终态错误及data-only JSON/type事件、六段展示；Raw返回类型尚无兼容桥 |
| `intent/structured.py`及`presentation/intent_recognition.py` `_completed_question_for_display` | V1生成/展示完整问题；V2缺对应产品合同 | QUESTION_COMPLETION_CONTRACT_GAP继续开放，本轮不实现 |

当前没有V2→V1重解析fallback接线，因为公共入口本来就是V1；未来把V2自然语言交回该入口会重新决定语义，不能称为无损计划适配。公共执行传输可供后续复用审查，但不能声称本轮已有V2复用且语义不变。

## 门禁与唯一下一行动

| Gate | 结论 |
|---|---|
| FRESH_TASK_INITIALIZATION | PASS_ON_TESTED_FROZEN_SCOPE_QUERY，4/4；范围限该原始首问 |
| FRESH_CHAIN_A | FAIL_STRICT_RELATION_AXIS；必要状态与barrier轴通过 |
| FRESH_CHAIN_B | PASS，4/4 |
| FRESH_CHAIN_C | FAIL；C2字段期待缺口，C3历史返回实际拒绝 |
| INTERNAL_DEMO_SEMANTIC_READY | NO |
| CONTEXT_FOLLOWUP_READY | NOT_READY |
| PLATFORM_EXECUTION_CHAIN_AUDIT | COMPLETE_WITH_WIRING_GAPS |
| READY_FOR_INTERNAL_DEMO_E2E_SMOKE | NO |
| QUESTION_COMPLETION_CONTRACT_GAP | OPEN_UNCHANGED |

单路径E2E建议：**当前不建议直接发起执行**。首问已有语义计划证据，但V2→执行、结果验证和回执保存尚未接通，该查询的真实SQL正确性也未验证。应先明确已有typed plan到执行适配器的隔离接线与Scope/Pin/结果证明边界；不直接查询生产数据。

下一步唯一最重要行动是调查C3的**历史Task引用mention与当前显式subject义务边界**，保留原始输出、原期待及显式槽位保护，不能删掉marker/slot来取得通过；本轮不启动。地区粒度期待、销售额时间口径、独立QueryShape、Dataset/DryPlan、完整问题展示及正式切流门禁继续保留。本轮提交和Draft PR完成后停止，未达到READY_FOR_USER_APPROVAL，不申请或执行V2替换。
