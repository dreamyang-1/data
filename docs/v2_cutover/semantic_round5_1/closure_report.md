# Round 5.1 — Live Follow-up Readiness

**ROUND_5_1_LIVE_FOLLOWUP_READINESS_PARTIAL**

**DECISION_C_FAILURE_IS_DOWNSTREAM**

本轮未修改 Production，也未增加 Temporary Context Resolver。9个真实执行轮中的6个失败执行点全部位于Context之后；没有证据支持用另一个Context模型处理这些失败。这个结论限于已观察的失败，不能据此证明尚未执行的地区追问、历史返回或真歧义已经正确。

Baseline HEAD：`76da2d0818dafe851ed2b2dc345ec5cf972242fe`。开始时开发目录和Git镜像的1566个tracked file一致、Git干净，Round5证据校验PASS。独立分支：`semantic-live-followup-round5-1-20260910t032044z`，父Draft PR #59。Final HEAD由`git_commit_manifest.json`的introducing-commit命令解析，push/PR回执保存在提交外；不递归把提交自己的SHA写入正文。

## 当前真实运行路径

Current Question → `RawTurnPlanner._run`（recognition.py:253）→ `discover_context`（context_proposal.py:51，最多4个候选）→ `RecognitionModelClient.complete`（recognition_client.py:19）→ `ContextAwareParse.context_proposal` → `accept_proposal` / `validate_proposal` → `CurrentTurnParser.parse`及已有Mention修复 → `_candidates` → 第二次模型`v2_semantic_edits` → `ScopedPlanSession.bind`及原生源值Lookup/Probe → `_patch`（recognition.py:542）→ `apply_task_patch`（slot_reducer.py:166）→ TaskSemanticState → `materialize_payload`（recognition.py:205）→ `resolve_turn` / `compile` → LogicalPlan / sealed state。

旧`pipeline.TurnResolver`仍有直接调用方，但Raw主链采用经过Hard Validator验证的联合Proposal。候选合法性、Scope、版本、Pending、当前显式输入和Reducer约束保持原样。本轮未改Prompt、Schema、Candidate策略、权重、关键词或阈值。

本次调用真实RawTurnPlanner和真实模型；Catalog通过现有原生内存Publication适配器加载冻结快照，源值使用已核验的只读观察回执。没有用Mock Recognition输出；没有执行SQL、写生产Redis/业务状态或访问真实查询结果。正式HTTP仍为`api.invoke` → `workflow.ainvoke` → `WorkflowNodes.skill_dispatch`调用`DataAnalysisOrchestrator.handle`（graph/workflow.py:68）；本次不是HTTP/SQL/最终回答的完整生产压测，也不是plan-only Shadow。

## 场景、分母与结果

在模型运行前冻结12类主场景和8个自然表达变体，共20个PUBLIC_DEV场景、44个声明轮次。完全相同的前置问题仅执行一次并复用其实际封存状态；失败前置不会注入成功状态。主集合实际执行7个唯一轮次，触达20个场景的前置/当前轮。当前轮仅2个真正执行：Pending回答和Pending新话题。

| 集合 | Case | 声明轮次 | 本轮唯一执行轮 | PASS | FAIL | BLOCKED | NOT_RUN |
|---|---:|---:|---:|---:|---:|---:|---:|
| 原始场景及自然变体 | 20 | 44 | 7 | 1 | 1 | 18 | 0 |
| 补充真实ADD→REMOVE链 | 1 | 2 | 2 | 1 | 0 | 0 | 0 |

BLOCKED18是已执行前置失败，不是18次模型关系判断错误；这18个当前追问轮未执行。补充链直接承接本轮已经成功的Pending答复计划，没有改写其指标、Scope或历史；不回填原始失败Case。全轮共9个唯一请求、20次模型调用，不合并成“2/21整体准确率”。PASS仅指独立声明的相关语义轴，不认证Whole Plan、DryPlan或执行结果。

Pending前置使用用户明确给出的“销售额/销售数量”选项，绑定冻结目录中的`含税销售总额`和`销售总数量`。通过现有typed Task/Pending/Resume、native seal/restore创建；**没有声称该追问由本轮模型先前产生**，也没有新增Catalog别名或伪造执行回执。

已执行且核对成功的真实链：Pending回答“销售额” → “订单数也一起看” → “销售额先不要”。三个计划的指标集合依次为销售额、销售额+订单笔数、仅订单笔数；同一合法任务、TaskState/Plan一致，无额外地区、时间、维度或实体继承。Pending→“江苏有哪些医院？”被正确识别为NEW_TASK，但下游未消费地区源值请求，未生成完整计划。

## 唯一First Divergence

| Stage | 独立失败执行轮 | 受影响主Case | Case IDs | 证据与边界 |
|---|---:|---:|---|---|
| SLOT_OPERATION | 4 | 7 | B、B2、C、C2、D、D2、J | Parse无操作marker但Draft使用ADD；无基础TimeSpec却提交时间组件修改；源值请求没有对应Filter edit |
| CANONICAL_BINDING | 1 | 4 | A、E、A2、A3 | 源值字段请求同时选了城市ATTRIBUTE和城市DIMENSION；Lookup要求ATTRIBUTE，实际在source_value_binding.py:14拒绝 |
| ENTITY_VALUE_GROUNDING | 1 | 8 | F、G、H、K、L、F2、G2、H2 | 所选city.city_name精确查找为空；冻结Probe回执complete=false，原64项边界安全拒绝 |

完整Case ID均带`LF51-`前缀。6个失败执行点不是6个已证明相互独立的生产Bug。Grounding的拒绝边界已证明；字段选择、源服务合同或数据覆盖的最终责任仍UNKNOWN，不能直接说Catalog损坏。冻结源值回执12次读取均命中，不是缺fixture导致该Probe拒绝。

每个失败Case只有一个主分歧，后续影响单列`downstream_effects`。J的RELATION_LIST/缺关系问题保留为下游QueryShape调查候选，没有再统计为第二个主Root。D原始“订单/笔数”边界由现有代码修复为“订单笔数”，不能把已恢复的raw错误再计为最终Mention失败。

| 错误轴 | 本轮已观察结论 |
|---|---|
| Relation / Target | 各0个错误；9轮包含5个空历史NEW_TASK和4个带上下文请求，不能宣称9轮都是追问 |
| 有上下文的Relation / Target | 各0/4：Pending回答1、Pending新任务1、Active ADD/REMOVE2 |
| Question Completion | 不可评分；V2输出合同缺失，不记成0个正确或9个语义错误 |
| Mention Boundary / Semantic Role | 最终规范化语义中未证实主分歧；raw role hypothesis不自动算硬角色错误 |
| Slot Operation / Binding | 主分歧分别4/1个执行点 |
| TaskPatch / Reducer | 没有证实独立Reducer错误；成功的3轮观察一致，拒绝轮未完成不能推断通过 |
| IR / Plan | 3个接受计划的已声明语义轴一致；其余未到达，不认证Whole Plan |
| Pending | 已观察答复和新任务关系均正确；新任务完整执行仍失败 |

## 完整问题合同

`RawTurnPlanner`的`RecognizedPlan`包含parse、resolution、plan、next_state、plan_state、edit_trace、context_trace及版本，没有completed/rewritten/resolved/canonical question字段。现有`completed_question`/`rewritten_question`在Legacy Structured Intent、AnalysisRequest和presentation路径，未接入该Raw链。

因此记录 **QUESTION_COMPLETION_CONTRACT_GAP**。当前不能验证“独立完整业务问题”与TaskState/IR的一致性，也没有用原问题或Task标题冒充补全问题。这是独立能力缺口，不是这6个下游失败的共同原因。本轮不补展示字段、不重写UI或生产接口。

## Native性能基线与Resolver决策

固定qwen3.7-max、Thinking=false、temperature=0、retry=0、评测timeout=60s；本地配置文件未修改。当前配置模型名与实际20个HTTP200响应一致。seed/top_p/max_tokens未发送，不宣称确定性。Scope81/[205]，时钟2026-09-09T09:00:00+08:00。

| 指标 | Native V2 Before，本轮n=9请求 | Native+Resolver After |
|---|---:|---|
| calls/request均值 | 2.22 | 未测试，不适用 |
| 输入Token均值 | 21,967.78 | 未测试，不适用 |
| 输出Token均值 | 1,015.11 | 未测试，不适用 |
| 首次Recognition mean / p95 | 10.09s / 13.67s | 未测试，不适用 |
| Raw计划请求 mean / p95 | 16.96s / 24.98s | 未测试，不适用 |
| Provider timeout / Schema failure | 0 / 0 | 未测试，不适用 |
| 新生成Clarification | 0 | 未测试，不适用 |

普通路径结构仍为2次、精确Pending答案1次；本轮另有3次既有源值Probe选择调用，所以实际均值2.22。不是新增Context Resolver调用。计时包含当前观察器及冻结适配器，不包括HTTP/V1/SQL/答案交付。失败请求也计入n=9，未仅挑成功3轮做总性能结论。Round5之前的旧生产延迟继续UNKNOWN，本轮不同语料不能补造那个Before。

未满足临时Resolver实验入口：没有观察到它可针对的Relation/Target共同错误。已存在的4个有上下文请求都选对关系/目标，失败主要是源值/槽位合同。因此没有比较辅助模型，没有选择Context Primary，trigger mode不适用，新增Context调用0。仅qwen3.7-max本轮可用性已实测；DeepSeek V4 Pro、GLM-5.2未评估，不能宣称MODEL_NOT_AVAILABLE或比较排名。

## 验证、剩余门禁与停止点

现有Context Proposal专项25、Context Slice8（18 turns）、Critical160，共193项重新运行通过。新增4项评测记账反例通过，防止共享失败历史、预算中断和未标注计划被算PASS。合计197 passed、0 failed、collection errors0；193个重跑旧节点的old-pass→new-fail0。首次pytest启动因异步插件重复注册退出，发生于collection之前；修正隔离runner环境后正式运行通过。没有重跑全量或重型Oracle/Replay。

Round3 Target15/15复用Round5冻结回执；全部V2源码未变，未谎称本轮重新执行。Round5全量3266/27仅作为基线。所有1566个原tracked file、V1、Oagnet、SQL Translator、Schema、Prompt、Reducer、UI、API/SSE和.env内容保持不变。没有Blind、Benchmark、Shadow、Canary、8088启停或切换。

| 能力 | 当前结论 |
|---|---|
| ADD / REMOVE | 本轮真实无地区/时间链通过；原含地区/时间场景仍被前置失败阻塞 |
| 地区替换 / REPLACE TIME / CORRECTION / CLEAR屏障 | 组件回归通过，指定live链尚未执行完整 |
| Historical Return / 真歧义 / 自包含新问题 | 指定live当前轮被前置失败阻塞，仍UNKNOWN |
| Pending回答 | 用户声明的typed前置+真实当前轮通过 |
| Pending→New Task | 关系和防劫持通过；下游完整计划失败 |
| Scope Safety | Critical及Context跨Scope回归通过；3个接受计划Scope一致，无新增授权逻辑 |

`CONTEXT_ATTACHMENT_CORE_READY=PASS_ON_IMPLEMENTED_CONTRACT`；**CONTEXT_FOLLOWUP_READY=NOT_READY**；`READY_FOR_USER_APPROVAL=NO`。不建议进入V2 8088 DIRECT REPLACEMENT PREPARATION。

最短后续阻塞路径是先审查初始化/源值Filter的Parse–Draft操作消费合同，以及源值字段类型和Probe覆盖边界，再重新执行本轮冻结的原始多轮场景。完整问题输出合同另行定义。**本轮只登记这些Root，不修改Production、不执行下一Round。**
