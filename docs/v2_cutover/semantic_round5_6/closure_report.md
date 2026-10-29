# Round 5.6 — Fresh Task Initialization & New-task Resolution

**ROUND_5_6_FRESH_TASK_CLOSURE_PARTIAL**

本轮完成了两项最小修改及验证、收尾。尚未达到内部演示语义门禁：三条独立 fresh chain 的首问均安全拒绝，后续8轮未执行。没有开始下一 Root、执行 SQL/E2E、修改 API/SSE/UI 或切换 V1。完整版本、逐轮结果、调用与证据 hash 见 [results.json](results.json)，回滚见 [rollback.md](rollback.md)。

## 版本与证据边界

开发目录 `E:/YouoAgent/DataAnalysis_Agent`；Git 目录 `E:/yy`。基线为 PR [#64](https://github.com/dreamyang-1/data/pull/64) / `564101f7e83964a5b27ff09e00db4199b0b5d847`，开始时 OPEN/DRAFT/未合并，1664个 tracked file 无意外差异。分支为 `semantic-fresh-task-round5-6-20260910t081523z`，Draft PR 堆叠到 #64 对应分支。

Root A 提交为 `f782d9f7997783658c5c1f68b91a822529eb278c`。Root B及最终证据提交可用 `git log --diff-filter=A -1 --format=%H -- docs/v2_cutover/semantic_round5_6/results.json` 精确解析；最终远程 SHA/PR 由发布后回执核对，避免在提交内递归写自己的 SHA。清单仅含3个生产文件、1个现有工具、2个新增测试文件及3个交付文档。

开始时间为2026-09-10 08:15:23 UTC，硬停止时间10:15:23 UTC。固定 Scope81/[205]、as_of=2026-09-09T09:00:00+08:00、qwen3.7-max、Thinking=false、temperature=0、retry=0；未修改 `.env` 或模型配置。冻结目录及源值观察通过原生 Pin/Scope 验证；没有新的源 SQL、目录写入或生产状态写入。

本轮先试验了受限单元素 SET 兼容，随后撤回：新 Live 三次都已返回数组，兼容并非这些输入所需；同时它违反一项既有“SET 必须完整集合”的反例。保留中间回执和原始输出，**没有修改该旧测试断言**。最终代码仅保留生成 Schema 修复和 Context description 修改。

因此必须区分版本：三条 fresh 首问和四条 Context Live 诊断发生在候选v1；最终v2撤回上述未使用的兼容。最终 Runtime 对三条新 capture 的实际请求、Schema及结果全部匹配，3/3仍在同一合同拒绝；这属于冻结输出复核，**不是最终版本的新 Live**。原始 Round55 capture 的输入版本不同，仅作为版本化 Runtime/Oracle 诊断。最终v2没有重启 fresh批次，因为已发现两项范围外阻塞，按本轮停止条件收尾。

## 两项 Root

| Root | 原始输出与消费要求 | 最早偏离及证据 | 最终改动与结论 |
|---|---|---|---|
| A | `metrics SET.value` 是单个 binding-handle对象；Registry要求 `list[BoundSemanticRef]` | **PROVEN A1/A4**：模型实际收到的 `SlotEditDraft.value -> JsonValue` 允许对象，缺少 slot+operation 联合约束；`_patch` 的 TypeAdapter拒绝 | 从既有 Task/value Schema 导出 metrics/dimensions SET 数组及严格 handle元素约束。未放宽 Runtime输入。新 Live 3/3 metrics 为数组；首任务整体仍0/3通过 |
| B | 原C为独立新查询，模型却提案 MODIFY已有江苏订单任务；随后字段不兼容拒绝 | 原始 ContextProposal 已错误，`OVERRIDE_DIRECTION=NONE`；Task摘要完整，未发现 Adapter改写正确模型答案 | 替换现有 Context description 的一处通用说明。四条新 Live 关系/目标轴4/4符合期待，但未验证C的完整 fresh新任务及历史返回 |

Root A 的旧 singleton REPLACE 适配仅适用于真实已恢复目标及版本；旧 initialization 仅把新空任务中有当前证据、没有显式 operation marker 的 ADD 转为初始化，不适用于已经声明的 SET。最终没有新增第二套适配，没有 dict→list 通用转换，没有元素默认、角色重写或 Scope放宽。Schema可拒绝对象并不意味着模型必然遵守，原有完整 Runtime校验继续保留。

原始失败输出在最终代码下仍因 scalar SET 报 `V2_CONTRACT_VALIDATION_FAILURE`。仅把 metrics对象改为单元素数组的 Oracle 随后报 `V2_SLOT_OPERATION_CONFLICT`；它揭示同一 capture 后面的 Filter操作冲突，不能算整个原失败已修复，更不能虚构完整 State/IR/Plan等价。原生合法单/多元素数组及负向校验由新增测试验证。实验兼容阶段的结果和撤回理由单独记录，不覆盖旧 capture。

Root B 的说明强调：Task候选只是可选引用数据；独立成立且无引用/编辑意图的请求，不因主题相似或可算差异而成为 MODIFY；明确编辑即使句子完整也可修改旧任务；省略追问可引用合法目标。已有历史候选、真歧义及 Pending映射合同保留。没有具体城市/指标特判、关键词判断、confidence常数、额外模型调用或规则覆盖。独立 Parse/Draft Prompt常量未变，**Schema description属于有效模型输入变化**；实际 Prompt、Schema含description、Candidate/context及模型参数分别记录版本/hash。

## 新 Live 与最终 Runtime复核

沿用 Round55 fixture的 case ID、原文及 expected；C历史返回正式原文为“返回刚才江苏订单笔数那个任务”。三条链分别新建独立 conversation，初始 Task/Pending/plans/history均为空，不共享任何成功 TaskState或执行前缀。

| Chain | 第1轮 | 后续轮 | 唯一首分歧 |
|---|---|---|---|
| HC55-A（4轮） | FAIL_SAFE_REJECT | 3轮 BLOCKED_HISTORY | Parse声明Filter SET，Draft给出Filter ADD：`V2_SLOT_OPERATION_CONFLICT` |
| HC55-B（4轮） | FAIL_SAFE_REJECT | 3轮 BLOCKED_HISTORY | SourceValueRequest存在但没有消费它的Filter edit：`V2_SOURCE_VALUE_REQUEST_NOT_APPLIED` |
| HC55-C（3轮） | FAIL_SAFE_REJECT | 2轮 BLOCKED_HISTORY | 同上，`V2_SOURCE_VALUE_REQUEST_NOT_APPLIED` |

合计11个声明轮，3个新执行轮，0复用轮，0通过轮，3失败轮，8个后续轮未运行。没有接受或发布计划；Patch/State/IR/Plan缺失只记 downstream effects。未用一次成功前缀覆盖失败或继续拼接历史。最终代码冻结输出复核保持这三项拒绝，Scope和完整模型输入相同；没有因 Runtime类型适配撤回而产生新的分歧。

为单独验证 Root B，恢复 Round55真实、合法、已接受的第一任务回执，运行4个新的一阶段模型请求及原生 Context验证，然后在 Draft前停止。它们明确标为诊断，不能算 fresh chain，也不能算Whole Plan通过。

| 新 Live Context诊断 | 模型/最终关系 | 目标 | 结果 |
|---|---|---|---|
| 原C：查询今年北京市销售总数量 | NEW_TASK | 无旧目标 | PASS_CONTEXT_AXIS |
| 独立反例：查询去年上海市销售总数量 | NEW_TASK | 无旧目标 | PASS_CONTEXT_AXIS |
| 明确编辑：把刚才的查询改为今年北京市的订单笔数 | MODIFY | 原江苏订单任务 | PASS_CONTEXT_AXIS |
| 省略追问：那北京市呢 | MODIFY | 原江苏订单任务 | PASS_CONTEXT_AXIS |

这些诊断没有改写 NEW_TASK期待，没有以“MODIFY也算通过”放宽评分。7个当前可观察关系决策（3首问+4诊断）均直接接受模型合法提案，hard/soft veto均0，rule-only和fallback均0，覆盖不是对抗性安全评测。四例是小规模公开诊断，不证明模型总体泛化，也没有证明 fresh C 的新任务发布和历史返回已通过。ADD/REMOVE/历史返回只由本轮离线 Context Slice提供组件回归证据。

## 回归与性能

最终受影响集合 **650 passed /0 failed /0 collection errors**。与实际 Round55回执逐nodeid比较，原618项全部保留通过，新增32项，old-pass→new-fail=0，缺失旧测试=0，旧测试源码/期待修改=0。其中 Critical160/160、Context8条/18轮均在本轮重新执行。旧 Target15/15在最终代码重新执行，属于版本化 Recorded signal migration，不是新 Live准确率或原请求严格Replay。

中间结果没有隐藏：实验兼容版本646/647通过，唯一既有失败是scalar SET拒绝反例；撤回兼容后该测试原样通过。新测试开发时还暴露嵌套非法handle的既有TypeError诊断形式，已记录为诊断债务，未修生产或接受非法值。私有复核脚本初次比较完整error wrapper时发现 `context_trace=None` 形式差异，后来只比较明确的type/reason并保留其他原始证据，不改变业务评分。

Agent/Oagnet/SQL Translator全量回归均 **NOT_RUN_FRESH_CHAIN_GATE_FAILED**，遵守“三条 fresh通过后才运行全量”的条件。没有把历史全量数字计为当前结果。跨服务生产修改为0。

本轮真实模型请求11次：Smoke1、fresh首问6、Context诊断4；HTTP200=11，鉴权错误/超时/重试均0。输入tokens125216、输出6727，总计131943。三个fresh请求总耗时分别22.047s、18.454s、16.781s；两个模型阶段均单独记录。四个一阶段Context诊断耗时12.625s、10.453s、15.187s、7.375s。Smoke耗时及其他原生阶段没有独立计时，不补造数值。小样本观测p95分别22.047s和15.187s，不是生产SLA或性能改善证据。

所有Oracle/冻结输出复核/离线测试的模型调用、SQL调用与生产写入均0。Live只调用既有模型端点，使用冻结Catalog/源值观察；不执行业务SQL。正常生产路径没有新增模型调用。

## 安全、门禁与停止点

错误模型Target提案：当前已观察轴0/7；已发布错误状态0，但发布状态分母也为0。不能把未发布状态表述为完整继承/字段/IR安全验证通过。CLEAR/REMOVE防复活、独立新任务发布后历史返回、Pending、真歧义都没有本轮 fresh接受链覆盖；组件通过不抵销此缺口。Source/Scope/Catalog硬约束未改，API/SSE/UI、V1生产路由和8088保持原状。

| Gate | 状态 |
|---|---|
| SET_GENERATION_CONTRACT | PASS_ON_EXPORTED_SCHEMA_AND_OBSERVED_ARRAYS |
| FRESH_TASK_INITIALIZATION | NOT_READY |
| NEW_TASK_RESOLUTION_ON_TESTED_CASES | PASS_CONTEXT_AXIS_ONLY，4/4 |
| FRESH_CHAIN_A / B / C | 全部 BLOCKED_HISTORY |
| REGRESSION_STATUS | PASS_FOCUSED_AFFECTED_ONLY；全量未运行 |
| INTERNAL_DEMO_SEMANTIC_READY | NO |
| READY_FOR_INTERNAL_DEMO_E2E_SMOKE | NO |
| CONTEXT_FOLLOWUP_READY | NOT_READY |
| QUESTION_COMPLETION_CONTRACT_GAP | OPEN_UNCHANGED |

下一优先级仅登记，不在本轮启动：**P1 Filter初始化显式SET与ADD edit消费合同**（本轮A及旧原始首问有证据）；**P1 SourceValueRequest与Filter消费绑定**（本轮B/C）。两者当前均安全拒绝，未证明静默错误查询，不把reason-code相同直接叫作已确认独立生产Bug。还保留原有目录时间、QueryShape、Dataset/DryPlan及生产切流门禁。

本轮因出现两项范围外首分歧停止扩展，不通过反复Live或改题凑验收。完成明确文件同步、两项可审查提交和堆叠Draft PR后停止。未达到 `READY_FOR_USER_APPROVAL`，不启动内部演示E2E、正式Benchmark、Blind、Shadow、Canary或V2替换。
