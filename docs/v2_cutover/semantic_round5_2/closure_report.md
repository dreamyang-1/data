# Round 5.2 — Downstream Semantic Blocker Closure

**ROUND_5_2_DOWNSTREAM_CLOSURE_PARTIAL**

原 D 场景已真实打通：**查询去年江苏订单笔数 → 不限地区 → 换今年**。地区清空后未复活，指标保持订单笔数，时间从2025变为2026，TaskState与Plan一致。但原18个BLOCKED场景仅1个获得完整当前轮Live覆盖，不能宣布完整Follow-up Ready。

Baseline为`bc5bc3f418c374f3f155403550649b23c64ce4e5`，开始时Git干净、1581个tracked file与开发目录一致。已核对PR #59/#60均OPEN/DRAFT/未合并，SHA及真实Git ancestry成立；Round5.1冻结证据校验PASS。分支为`semantic-downstream-round5-2-20260910t040418z`，Draft PR堆叠到#60。Final HEAD由`git_commit_manifest.json`的introducing-commit命令解析，提交后的SHA、push及Draft PR验证回执保留在提交外，避免递归写入自己的SHA。

## Root Clustering与因果证据

原6个失败执行点归为5个调查簇，**4条SLOT_OPERATION先分为3簇**，没有当成一个Bug。详见[root_clusters.json](root_clusters.json)，每个原Case包含原问题、期待、Relation/Target、Mention/Role、操作、选中字段、原始拒绝位置和唯一Root。

| Root / 原Case | 单一Artifact Oracle | 最终处理与边界 |
|---|---|---|
| RC-S1：初始化赋值表示，B/C | B仅将初始Filter ADD改为整槽SET，进入`CATALOG_METRIC_TIME_ANCHOR_MISSING`；C仅将多指标ADD合为一个SET，进入`V2_TEMPORAL_BASE_REQUIRED` | PARTIAL。空NEW_TASK的合法显式ADD可合为一个SET；C已越过原操作边界。B还包含结构化ADD不允许的CURRENT_DATASET scope，保留原拒绝，不能借转换绕过。 |
| RC-S2：初始TimeSpec，D | 仅替换初始TimeSpec lowering产物，使用当前范围及Catalog声明的指标anchor，得到PLAN | CLOSED。原始录制输出及真实D前置均生成计划。 |
| RC-S3：源值请求未被Filter消费，J | 仅在Draft增加消费其现有request_id的Filter，进入未录制的value-choice阶段 | OPEN。不自动猜EQ、不静默补条件；没有将缺少后续模型输出当作Oracle成功。 |
| RC-B1：Source字段类型，A | 仅移除所选DIMENSION，转为城市Probe上限拒绝 | 生成Schema已缩窄到ATTRIBUTE；消费端仍拒绝非法维度。Live A选了ATTRIBUTE，但Parse候选角色也变化，不能把此次结果全部归因于Schema或宣布模型根因关闭。 |
| RC-V1：源值覆盖，F | 仅改为另一个已提供的字段假设，进入未录制的value-choice阶段 | OPEN / SOURCE_VALUE_GAP。city.city_name精确查询为空、64项Probe不完整；字段选择与源数据责任仍需证据。没有自动改成province或猜地名后缀。 |

基线6条全部严格匹配原模型输入并复现原拒绝。生产生成Schema改变后，最终6条是**版本化Recorded Output回归**：原输出不改、全部后续实际Runtime执行，第二阶段Schema差异明确记录，不冒充严格原请求Replay。最终原录制为1 PLAN/5拒绝：D通过，C进入正式Catalog时间口径缺口，其余保留边界。Oracle均不计模型/Gold PASS。

第一次Live D前置成功后，“不限地区”暴露出一个新表示问题：Parse为REMOVE，Draft对同一当前Filter子树为CLEAR、value=null。现有`structured_edits.filter_edits`对这两种整子树删除采用完全相同的lowering。单点Oracle只将Draft操作对齐为REMOVE，得到正确清除及屏障；随后实施最小对齐并真实重试。此为RC-S4，**CLOSED_ON_RECORDED_AND_LIVE**，没有修改Context relation或Reducer。

按“5个原簇+1个新簇”统计，完全关闭2个Root，未完全关闭4个（含部分改善与生成合同收窄）。这不是全项目独立Bug数量下界。没有证实更早的Mention/Role共同Root；D原始“订单/笔数”仍由既有完整指标Mention恢复合同修复。

## 实际生产修改与自审

Production文件仅：

- `app/semantic_v2/recognition.py`：接入表示适配、初始时间lowering及第二阶段生成Schema。
- `app/semantic_v2/recognition_initialization.py`：新增最小适配器和生成视图。

初始合并仅适用于target不存在、base=0、prior确为空的新Task；无显式操作冲突，原始每条edit均由自己的当前Mention证明，绑定handle也必须属于该edit自己的证据。多指标ADD合成一个SET，避免多个SET导致前项丢失。模型显式声明REMOVE/CLEAR、历史目标、混合操作、错误role、伪造handle和跨Mention借用不能通过这个适配器。

时间初始化仅接受当前明确的SET组件，验证原始值类型、当前范围/粒度表达及Catalog时间anchor。不存在正式anchor继续拒绝。字段未显式指定时，不让模型发明TIME_FIELD handle；显式字段不能被默认anchor覆盖。现有完整TimeSpec及已有任务的组件修改路径保留。

整子树删除对齐只适用于当前已验证Task版本、同一合法target_handle、value=null、每个证据Mention操作一致的REMOVE/CLEAR。它不适用于带值成员REMOVE、指标集合CLEAR、其他目标或其他操作。原操作guard、绑定校验、Scope、lowering、CLEAR屏障和Reducer仍实际执行。

复审补强了“先验证每条原始edit证据，再合并证据集合”的边界；新反例防止把属于另一Mention的绑定借助合并变合法。所有业务词只出现在公开评测案例/诊断材料，Production新增业务关键词、Regex、confidence阈值均为0。

**PARSE_PROMPT / DRAFT_PROMPT文本修改0**；Context Proposal/Context Schema/候选上限4/Hard Validator/Target arbitration修改0。第二阶段生成Schema有明确版本`v2-semantic-initialization-generation-v2`：限制源字段enum，并为无TIME_FIELD handle的初始范围提供组件表示；新增1个Schema说明单元。不能将Schema说明伪装成“模型输入完全未变”。公共模型Schema、HTTP/API/SSE、输入输出模板不变。

## Live复测与分母

本轮11次qwen3.7-max调用，5个请求attempt、4个唯一turn。顺序为D前置、首次CLEAR拒绝、A前置拒绝、修复后的CLEAR、换今年。3个计划成功，2个拒绝attempt全部保留，不只报告成功样本。

修复后的CLEAR/换今年直接恢复本轮真实D前置的sealed state，未更改Task ID、Scope、指标、条件或伪造成功历史。该前置和两个成功追问均在最终Runtime上严格匹配输入重放，确认语义结果一致。它们仍是不同时间采集的真实请求及版本化复用，不能声称整个链一次采集于同一个静态Runtime版本。

| 原5个前置 | 本轮Live | 原因 |
|---|---|---|
| D：去年江苏订单笔数 | PASS | 正式order_count时间anchor为sales_order.created_date |
| A：去年上海销售额 | FAIL | 选中城市ATTRIBUTE后，源值Probe不完整 |
| B：去年江苏销售额 | NOT_RERUN | 保留操作/Catalog blocker及预算边界 |
| C：去年江苏销售额和订单笔数 | NOT_RERUN | Recorded Output已证实缺少销售额Catalog时间anchor |
| F：上海销售额 | NOT_RERUN | 原城市源值覆盖拒绝保持 |

即前置**1成功/1失败/3未重跑**，不能写成5条全部复测。完整原始12类及变体的输入和标签没有改成更容易的版本；本轮仅调度其中的已证实解锁路径。原20个Case按本轮Live覆盖为1 PASS、4 BLOCKED（共享A的同一失败前置）、15 NOT_RUN。不是20条都已执行，更不是对Round5.1准确率的直接比较。

原18个BLOCKED中，**1/18**真正执行到完整当前轮并通过，包含2个真实成功Follow-up；其余17个仍无完整最终轮Live覆盖。预算在Live前已说明：5个前置加完整Follow-up需超过12次；最后剩余1次不足以完整开始另一个普通规划请求，没有消耗成半次失败请求。没有增加或测试Context Resolver。

| 能力/错误轴 | 本轮结论 |
|---|---|
| Relation / Target | 已观察3个上下文attempt、2个不同Follow-up，语义目标错误各0；REMOVE/CLEAR标签差异单列，不能说标签完全相同 |
| SlotOperation | 首次CLEAR有1次拒绝；同一原输出在最终Runtime及实际重试通过 |
| Binding | 本轮Live未再发生非法Dimension绑定；A的Parse候选变化构成混杂，不能宣称质量根因完全关闭 |
| Grounding | A仍有1次Live拒绝；F保留同源值边界，未Live重跑 |
| TaskPatch / Reducer / State / IR / Plan | 3个接受计划的指标、范围、字段、地区和原生默认项一致；未证实独立错误，未到达的Case不算通过 |
| CLEAR / REPLACE TIME | 原D真实链PASS：2025江苏订单笔数→2025不限地区→2026不限地区，clear barrier保留 |
| ADD / REMOVE / 地区REPLACE / Historical Return | 组件回归通过；本轮无相应完整Live重测 |
| Pending Response / Pending→New Task | 组件及历史证据保留；本轮未Live重测 |
| True Ambiguity / Self-contained New Task | 本轮未获得所要求的历史前置及当前轮Live覆盖 |
| Scope Safety | 3个接受计划为81/[205]，输入状态未原地变异，Critical及Context边界测试通过 |

全过程未执行SQL或生产写入。Frozen Catalog/Source observations是原生只读评测适配器，不能冒充真实数据库E2E、Dataset或最终结果正确率。

## 性能与验证

| 指标 | Round5.1 Before n=9 | 本轮After n=5 attempts |
|---|---:|---:|
| calls/request均值 | 2.22 | 2.20 |
| 输入Token均值 | 21,967.78 | 27,831 |
| 输出Token均值 | 1,015.11 | 882.20 |
| Recognition mean / p95 | 10.09s / 13.67s | 7.99s / 10.87s |
| Raw计划请求mean / p95 | 16.96s / 24.98s | 13.35s / 19.83s |

调用路径没有新增模型步骤。输入Token增加不能忽略：同一D前置的第二阶段context完全一致、候选仍51个，新增生成Schema使input tokens从17,170到18,007（+837）；首次Recognition仍为3,004。总体均值还受上下文attempt占比从4/9变为3/5影响。Probe句柄随运行pin改变，相关输入token另有小幅差异。不同小样本不能证明整体加速或退化；原始失败attempt计入性能分母。

11次HTTP200，Provider timeout0、Schema failure0；temperature=0、Thinking=false、retry=0，as_of固定2026-09-09T09:00:00+08:00。未设定/认证seed，不宣称确定性；没有正式Benchmark。

最终相关验证 **526 passed / 0 failed**：507个既有节点及19个新增测试。Context Slice **8/8、18 turns**，Critical **160/160**，Round3 Target **15/15**在最终Runtime重新验证；Target是版本化录制信号迁移，不是新模型准确率，13条Private仅机器回归，未人工打开调参，Blind未访问。old-pass→new-fail=0（507个重跑节点），collection errors=0。全量3266 passed/27既有failed只作为基线，**本轮未跑全量**。

## 剩余阻塞与停止

冻结目录明确显示：sales_total_including_tax的time_caliber.time_anchor=null；order_count声明sales_order.created_date。不能从共同sales_order实体依赖推断两者必须使用同一时间口径。需要正式Catalog/业务口径声明，不能为了查询通过在Agent抄入日期字段。此缺口只约束依赖该指标时间语义的查询，不是全部离线评测的全局前置。

城市字段精确值为空且Probe不完整，继续SOURCE_VALUE_GAP；未消费源值请求继续拒绝，模型QueryShape等后续问题只登记，未增加补偿。剩余跨业务能力的Live覆盖还不足。

**CONTEXT_ATTACHMENT_CORE_READY=PASS_ON_IMPLEMENTED_CONTRACT**

**CONTEXT_FOLLOWUP_READY=NOT_READY**

**READY_FOR_V2_READ_ONLY_E2E_SMOKE=NO**

QUESTION_COMPLETION_CONTRACT_GAP保留。V1保持正式路由；Oagnet、SQL Translator、Catalog、Redis、UI、公共API/SSE、8088、Blind、Benchmark、Shadow、Canary均未修改或启动。不是READY_FOR_USER_APPROVAL，不申请切换。本轮在提交、push和堆叠Draft PR后停止，不自动进入下一Round。
