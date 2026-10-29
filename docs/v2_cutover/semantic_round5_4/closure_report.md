# Round 5.4 — Filter Operation Contract Closure

**ROUND_5_4_FILTER_OPERATION_CLOSURE_PARTIAL**

已补齐 Filter 生成端对既有操作/operand 结构的约束，未放松消费端校验。有效 Live 证明 ADD、REMOVE 以及当前轮 CLEAR 删除与 barrier；地区替换仍因源字段与当前 Filter target 不匹配而安全拒绝。**CONTEXT_FOLLOWUP_READY=NOT_READY；READY_FOR_V2_READ_ONLY_E2E_SMOKE=NO。** 本轮停止，未执行 SQL E2E、8088 切换或下一 Round。

## Git 与基线

基线 HEAD `116ffe06d12812f849ba60eaa644df17cbc89f8a`，分支 `semantic-grounding-round5-3-20260910t052350z`。PR #62 为 OPEN/DRAFT/未合并，真实 head/base 与 #61 ancestry 均已核对；1620 个 tracked file 与开发目录一致，起始 Git 干净，没有接续遗留测试。开发目录 `E:/YouoAgent/DataAnalysis_Agent`，版本仓 `E:/yy`。

新分支 `semantic-filter-round5-4-20260910t063432z`，Draft PR base 为 #62。最终提交通过 `git_commit_manifest.json` 中 introducing-commit 命令定位，提交后另核对远端 SHA、PR head/base 和 Git 干净状态，不在提交内部循环写自己的 SHA。变更清单与逐文件 hash 见本目录 manifest。

## Filter Root 与 Oracle

实际路径和逐层事实见 [CURRENT_FILTER_OPERATION_PATH.md](CURRENT_FILTER_OPERATION_PATH.md)，机器证据见 [filter_root_evidence.json](filter_root_evidence.json)。

| 原失败 | 最早 Filter 产物问题 | 单变量 Oracle | 下一分歧 |
|---|---|---|---|
| Round5.3 live J | 新增条件只有源值引用，缺完整 Predicate | 仅补正确 Filter value；Scope、Pin、Context、Mention、候选和源值选择不变；越过原拒绝 | CATALOG_RELATIONSHIP_REQUIRED |
| 原 B | 新任务条件错误使用 CURRENT_DATASET | 仅将 Filter value.scope 改成已有合同的 CURRENT_TASK；越过原拒绝 | CATALOG_METRIC_TIME_ANCHOR_MISSING |
| 本轮 HC54-A | 合法 REPLACE 指向原 province Filter，源请求选择 city 字段 | 只改 SourceValueRequest selector 为已有 Filter target 后完整计划通过；字段 Grounding 随之改变，明确不是 Filter-only Oracle | 原拒绝 SOURCE_VALUE_FILTER_FIELD_MISMATCH |

原 J/B 的导出生成 Schema 均接受非法 operand，Runtime 的现有规则却要求不同的结构：分类 **C（导出合同缺口）+ F/G（Producer 的 operand/操作不一致）**。没有证明 Runtime Adapter 误改合法输入或 Schema 表达能力不足。本轮仅导出已有 typed operand 分支；内部类型没有新增 primitive，正确表达早已存在。不补 EQ、不改 target、不换字段、不改 Scope，也不把拒绝记成修复成功。

原 J/B 的 Filter-only Oracle 均未完整通过，故不能写成完整 `FILTER_OPERATION_CAUSAL_ROOT_CONFIRMED`。本轮 A 的源选择 Oracle虽然得到计划，也不计作 Live/模型/Gold PASS。它说明运行时字段一致性校验有效，剩余最短调查点是 **SourceValueRequest 字段选择与当前 Filter Target 的语义对齐**。没有为此加新 Prompt、规则或自动改字段。

原6个失败记录按当前生成版本回放：A仍非法源字段类型，B仍原 Filter 表示拒绝，C为目录时间缺口，D可生成计划，F缺老记录未采集的 value-choice 输出，J仍原未消费请求拒绝。新旧输入 Schema 差异显式标记为 versioned recorded-output regression，非严格同请求重采。Round5.3已接受 F 及关闭 targeted lookup 的因果对照保持原结果。原模型 Capture、Gold 和前阶段报告未改写。

## 修改范围

Agent Production **2 files**：新增 `filter_generation_schema.py`，`recognition.py` 增加该生成 Schema view 的接入。Oagnet Production **0**，SQL Translator **0**。

Prompt 文本 **NO**；Schema **仅已有 Filter operand 的生成约束**；Context **NO**；Grounding architecture **NO**；Reducer **NO**；Validator relaxed **NO**；Business keyword **NO**；V1/UI/API/SSE/8088 **NO**。正常路径没有增加模型阶段。Context 候选上限4、Source Value Top8、Scope、Pin、CLEAR/REMOVE barrier 均保持原实现。

## Catalog 与 Preflight

已再次只读采集真实81/[205]目录，与认证冻结快照完整内容及版本相符，catalog_version=`3f9589b95d376ebdf9683425dcf6c646f2394b278718b033527bd5e08b64fe21`。

| 对象 | 当前权威合同 |
|---|---|
| 订单笔数 order_count | COUNT(DISTINCT sales_order.order_key)，业务订单数；时间 sales_order.created_date |
| 销售总数量 sales_total_quantity | SUM(sales_order.quantity)，退货/冲销负值抵减；同一正式时间锚 |
| province / city | 存在受治理字段映射及冻结 exact 源值证据；使用当前 Scope/Pin |

字段、维度、公式、时间、源值和 scope 的自动检查见 [catalog_healthy_fixture.json](catalog_healthy_fixture.json)。本轮验证规划语义，不声称完成 SQL 可执行性或实体行唯一性认证。

销售额 `sales_total_including_tax` 的 **CATALOG_TIME_CONTRACT_GAP 仍成立**；没有添加默认日期。销售额时间案例不是 Context Engine Failure。

Live 入口在每次请求前检查 UTF-8、原文逐字匹配、fixture hash、request 序列化；开始前还验证模型ID/scope、当前目录与冻结 Pin、指标/时间合同、源值证据和 generation Schema。fixture 构建阶段发现的依赖路径、必传 application_id、冻结 source adapter 装配问题均在模型调用前拦住并修复；没有把这些当生产语义缺陷。

## Live 输入与配置错误单独记账

本轮首批4次请求均 HTTP401。诊断证明：预检导入 Oagnet fixture 时，`Oagnet/config.py` 加载工作区根目录 `E:/YouoAgent/.env`，其中通用 API_KEY 进入进程环境；后续才读取 Agent Settings，导致评测用了错误的凭据来源。**这是我本轮评测装配的配置隔离错误，非4个 Context/模型语义失败。** 请求文本编码有效，但这4次不能进入语义分母，调用预算仍计4次。

修复仅在评测工具：在 Oagnet fixture 导入前固定 Agent 原配置，401/403 后停止继续请求。密钥只在进程内供认证和相等性核对使用，未输出或记录；Agent与工作区根目录 `.env` 均未修改，没有改模型或生产配置。后续8次请求均HTTP200，4个有效业务轮取得完整原始观察。凭据来源时序已有回归保护，原401回执保留在 PRIVATE。

## Healthy Slice 与状态行为

声明 **11 Case /25轮**，包含用户要求的A–K。当前有效新执行 **4个唯一轮**：**3 PASS /1 FAIL**；另复用**1个真实历史前置轮**，没有计作本轮新 Live。Case 为 **2 PASS /1 FAIL /8 NOT_RUN /0 BLOCKED**；NOT_RUN 包括仅部分执行的链及只有无效配置尝试的Case。没有以到达前置或 NOT_RUN=0 伪造完整覆盖。

| Case | 能力 | 当前有效结论 |
|---|---|---|
| A | 地区 REPLACE | FAIL：字段/Filter target 不匹配，安全拒绝 |
| B | 时间 REPLACE | NOT_RUN：只有401配置错误尝试，无有效当前输出 |
| C | ADD | PASS：订单笔数+销售总数量，保留江苏和去年 |
| D | REMOVE | PASS：准确删销售总数量，保留订单笔数/江苏/去年；复用C真实ADD轮 |
| E | CLEAR→时间修改 | NOT_RUN完整Case：当前删除/Barrier PASS，后续时间轮未执行 |
| F | CORRECTION | NOT_RUN |
| G/H | NEW TASK / HISTORICAL RETURN | NOT_RUN |
| I/J/K | Pending→新任务 / 真歧义 / 自包含新任务 | NOT_RUN |

3个接受计划按独立业务合同核对完整指标集合、原地区/时间、目标任务、state/IR/LogicalPlan一致性、scope及清除barrier，均通过已声明轴。`不限地区` 的模型关系标签为REMOVE，原生 subtree 删除产生CLEAR barrier；这是既有Round5.2的删除等价合同，不是本轮改标签。没有后续Live轮便不能宣称 CLEAR/REMOVE Resurrection Safety 已覆盖。

Relation Error **0/4**，Target Error **0/4**，已接受计划 Wrong Inheritance **0/3**。Mention/Role/SlotOperation/FilterOperation 本轮有效输出错误均未观察到；**Binding字段兼容错误1**，其源头是SourceValueRequest选择字段，不重复算为多个Root。被拒绝A没有最终IR/Plan，不以未到达阶段当作通过。Pending Hijack、Historical Return、True Ambiguity、Self-contained和复活门禁仍未形成当前有效Live分母。详见 [live_coverage.json](live_coverage.json)。

## Regression 与复审

- Filter focused **124 passed**。
- 受影响Agent **425 passed**，评测入口/独立计划检查最终 **20 passed**；去重合并 Context/Critical 后 **604 unique passed /0 failed**，其中 **563既有 +41新增**。
- Context **8/8、18 turns**；Critical **160/160**；Round3 Target **15/15**，其中13条Private仅机器回归，无人工逐条调参。
- old-pass→new-fail **0**；collection errors **0**；旧测试 expectation 修改 **0**。
- Agent/Oagnet/SQL Translator 全量 **NOT_RUN**：本轮健康Live核心门禁未通过，未满足用户要求的全量启动前提。既有Agent3266/27只保留历史身份，未推算新全量成绩。Oagnet无本轮源码修改，未重跑其专项或全量。

正反例与9类错误注入约束了生成合同和已接受计划评分：额外指标、错误任务/关系、丢barrier、跨scope、错误时间/分组、缺IR观察、原地状态变异均不能误判PASS。完整测试节点与回执hash见 [test_delta.json](test_delta.json)。最终复审确认所有修改仅为生成view和评测侧代码，原消费端仍拒绝J/B以及本轮A；没有用测试绿灯掩盖401或未执行Live。

## Performance

有效4个请求：**2 calls/request**；input token mean **26725.75**，output **626**；recognition mean/p95 **11.480/12.203秒**；request mean/p95 **11.781/12.500秒**。全部12次调用为8次HTTP200、4次HTTP401，timeout **0**、schema failure **0**。401无token用量回执，不当0混入有效请求平均。

本轮Live为冻结源值观察，Grounding生产检索延迟 **NOT_MEASURED**；targeted调用0，A取得一个exact city候选后因字段兼容拒绝。上轮0.312/0.219秒是真实源读取的历史单独测量，未复用为本轮性能。样本不匹配，不能宣称相较旧轮提速；没有增加正常模型调用阶段。详见 [performance_receipt.json](performance_receipt.json)。

## Final Gates / Safety / Stop

QUESTION_COMPLETION_CONTRACT_GAP **OPEN_UNCHANGED**；CONTEXT_ATTACHMENT_CORE_READY **PASS_ON_IMPLEMENTED_CONTRACT**；CONTEXT_FOLLOWUP_READY **NOT_READY**；READY_FOR_V2_READ_ONLY_E2E_SMOKE **NO**。

最高剩余阻塞是当前源请求字段/Filter Target的对齐以及缺失的有效Live覆盖，不能宣称剩余只有Catalog Gap。未继续扩Grounding、重做Context或逐Case打补丁。

Blind accessed **NO**；Business SQL executed **0**；Production writes **0**；Catalog writes **0**；Index rebuild **0**；V1 changed **NO**；8088 changed **NO**。实际目录元数据只读查询已单列；本轮没有业务SQL E2E、Benchmark、Shadow、Canary、正式切流或下一Round。
