# Round 5.3 — Source Value / Grounding / Catalog Closure

**ROUND_5_3_GROUNDING_CLOSURE_PARTIAL**

城市源值路径已打通，原 F 前置“查询上海销售额”得到真实计划；销售额时间问题已归类为 **CATALOG_TIME_CONTRACT_GAP**。源请求消费器已存在且确实运行，J 不是缺少接线。另修复了源请求生成 Schema 未导出已有互斥约束的问题。**本轮没有达到健康目录 Live Follow-up 验收，不能进入 E2E Smoke。**

本轮我生成平行切片时发生 PowerShell 管道编码损失，3 个请求实际发送了问号。它们属于 **FIXTURE_GAP**，不能计为 Context/模型语义失败；3 次真实调用仍计入预算，原始回执完整保留在 PRIVATE。已修复 UTF-8 输入生成并新增独立文本校验、正式输入逐字比对及真实调用入口预检。正确输入升级为 v2，未覆盖或重新评分旧请求。剩余 1 次调用不足以完成正常两阶段请求，因此停止真实调用，没有隐性追加预算。

## Git 与范围

开始 HEAD 为 `bf3e8dbb9adb3abe40a4a53f8475f27799cf3da9`，Git 干净，1598 个 tracked file 与开发目录一致。已通过 GitHub API 核对 PR #60/#61 的实际 SHA、OPEN/DRAFT/未合并状态，并以 Git ancestor 检查真实依赖。

开发目录 `E:/YouoAgent/DataAnalysis_Agent`，版本库 `E:/yy`。分支 `semantic-grounding-round5-3-20260910t052350z`，Draft PR 基于 #61。最终 SHA 由 `git_commit_manifest.json` 中 introducing-commit 命令解析；提交后另核对远程 SHA、PR head/base 和全部 committed blob，避免提交内自引用 SHA。

Production 仅 5 个文件：

- Agent：`catalog_bridge.py` 增加内部 Pin 协议方法；`source_value_probe.py` 接入定向候选并记录检索耗时；`source_value_recognition.py` 导出已有字段/目标互斥约束。
- Oagnet：`catalog_publication.py` 增加当前 Pin 的最小候选适配及结束复验；`catalog_value_candidates.py` 复用已有源字段映射和只读连接。

Oagnet 的源读取问题在所属服务处理，没有把物理查询补偿塞进 Agent。SQL Translator 未改。Prompt、Context Proposal/候选上限4/仲裁、Reducer、CLEAR/REMOVE barrier、confidence、生产 Regex/业务词规则、V1、UI、API/SSE、8088、模型配置增量均 **0**。仅第二阶段 SourceValueRequest 生成 Schema 增加已有 XOR 合同，公共输入输出及 Context Schema 不变。没有第二模型、第二索引或第二源值存储。

## Source Value 当前路径与 J

完整八项回答见 [source_value_current_path.json](source_value_current_path.json)。Producer 为第二阶段 LLM `v2_semantic_edits`；对静态 enum 无法表达的当前 FILTER_VALUE，它通过 `SourceValueRequestDraft` 选择当前合法 ATTRIBUTE 字段或现有过滤目标。请求本身不声明 EQ/IN/ADD/REMOVE 等过滤语义。

真实路径为 `RawTurnPlanner._run → source_filter_patch → resolve_requests → lookup_source_values → select_probed_value → choice_handles/hydrate_choice → _patch → native reducer → TaskState/IR/Plan`。候选来自当前 Catalog Pin、受治理物理字段映射及真实源观察；绑定携带独立 SourceValueBindingEvidence。

原 J 已进入 Consumer，但 Draft 只有医院 subject 和 source request，没有引用 request_id 的过滤 edit，故 `V2_SOURCE_VALUE_REQUEST_NOT_APPLIED` 在检索前拒绝。新增正反例证明同一个现有 Consumer 在提供合法消费操作后正常工作。不能把请求存在推断为一个自动 EQ 条件。

本轮真实 J 已生成引用 request_id 的 edit，并实际完成江苏省候选选择；随后因初始 Filter ADD 的 value 只有值引用、没有合法 Predicate 而停在 `V2_SLOT_OPERATION_CONFLICT`。因此旧“未消费”现象不等于 Consumer 缺失，剩余是 **Producer 语义操作合同问题**。RELATION_LIST 等后续问题不重复计为第一根因。

## Grounding 与因果证据

原城市 Probe 忽略当前查询词，枚举所选字段最多65条以检测64项上限；高基数字段因此无法提供小候选集。只读审计发现 `dim_city.city_name` 已有 `idx_city_name`，定向查询 EXPLAIN 为 range。当前源数据证明相关候选存在，这不是“城市数据根本没有对应值”。

新增检索复用同一 Scope、实体、字段、数据源及 Catalog Pin。查询参数转义通配符，只有已有索引首列匹配且 EXPLAIN 证明 range/ref/const 才执行；返回最多 **8** 个候选，沿用已有 exact reader 的小集合预算。没有扩大64项枚举上限、扫描全 Catalog、建立新索引或传整表给模型。若定向结果也超过8项，或缺少可用索引，继续安全拒绝。

前缀匹配只是检索条件，不是别名声明或绑定依据。候选保持真实二进制拼写，不依赖数据库不区分大小写的 DISTINCT 合并。现有模型必须显式选择，随后再做 canonical exact lookup、字段/角色/Owner/Scope 验证及 Pin.finish 复验；Top1、fuzzy、名称和模型常识均不能直接成为授权绑定。

只读单变量 Oracle 将同一字段的枚举查询改为定向查询，取得1个真实候选；未假造缺失的第三阶段模型答案。随后真实 F 使用城市字段和该候选生成计划。对这份相同 Raw Capture，只关闭定向 Consumer 路径便重新得到 cardinality reject，开启则得到计划，证明这个通用引擎路径具有因果作用。Oracle 不计 Gold/模型 PASS。旧 F 记录没有第三阶段模型输出，仍保留 NEXT_DIVERGENCE，不替换旧分数。

F 的真实后续纠正请求同时提交字段候选和当前 filter target。旧生成 JSON Schema 接受该结构，Python validator 却拒绝，证明 **ENGINE_DEFECT：生成 Schema/Runtime 合同不一致**。现已导出同一个 oneOf 约束，未放松 validator。单变量 Oracle 仅选择已有 target 分支后越过结构校验，下一步缺少冻结 value-choice 输出；该 Schema 修复没有预算内的新 Live 重试，不宣称模型成功率已改善。

## Catalog 时间合同

已只读重查权威 Catalog、指标定义、维度 indicator 关联与字段映射；当前 catalog_version 与冻结版本一致。详见 [catalog_time_contract.json](catalog_time_contract.json)。

| 指标 | Catalog 时间 anchor | 结论 |
|---|---|---|
| sales_total_including_tax | null；仅关联 hospital 维度 | CATALOG_TIME_CONTRACT_GAP |
| order_count | sales_order.created_date | 可用于健康目录平行切片 |
| sales_total_quantity | sales_order.created_date | 可用于健康目录平行切片 |

`get_metric` 从正式时间维度关联读取唯一 anchor，Catalog Bridge 保留同一 metadata，V2 没有丢失已声明 anchor。现有 ASL 字段合法性与 V1 当前行为不能替代业务默认日期合同。需明确含税销售额的正式日期口径再治理目录；本轮 Catalog 写入 **0**、代码日期默认值 **0**。该缺口只约束相关带时间查询。

## 原四个 Root 与 Live 覆盖

| Round5.2 剩余 Root | 本轮状态 |
|---|---|
| RC-S1 B/C 初始化表示 | NEXT_DIVERGENCE：B仍为非法过滤操作表示；C明确进入 Catalog时间缺口 |
| RC-S3 J 源请求消费 | 证明不是 Consumer/Routing 缺失；真实消费后进入过滤操作表示冲突 |
| RC-B1 A 非法源字段类型 | 原 DIMENSION 仍拒绝；F证明合法 ATTRIBUTE 路径可用，未据此宣称 A 模型已关闭 |
| RC-V1 F 城市源值 | CLOSED_ON_INDEXED_ENGINE_PATH：实际前置计划通过；不保证所有别名和字段的召回完整性 |

新增 Schema XOR 合同缺陷已修，生成合同对齐与真实模型效果分开。新增评测编码缺陷已修预检，但 Live 覆盖未补回。明细及唯一第一分歧见 [root_closure.json](root_closure.json)、[live_coverage.json](live_coverage.json)。

| 集合/单位 | 本轮结果 |
|---|---|
| 原5类前置查询 | F 1成功；A/B/C/D 4未重新调用，保留已有 Catalog/原始记录及 D 的历史Live证据 |
| 原20 Case / 44声明轮 | 当前有效执行3个唯一轮：F前置、F纠正、J；Case为0 PASS /2 FAIL /18 NOT_RUN |
| 原18个 BLOCKED | 仅 F 本轮真实到达最终追问，但最终 Schema FAIL；不能写成新增1个通过Case |
| 新 Catalog-Healthy 10类 | 3个损坏输入尝试；有效Live轮0，语义PASS0/FAIL0；修正v2的10 Case仍NOT_RUN |
| Provider 请求 | 11次：有效业务输入8次、损坏输入3次；全部HTTP200，超时0 |

原场景、Gold 和 Expected 没有删除或更换。健康切片由当前订单笔数、销售总数量的正式时间合同及已证明源值建立，作为独立 PUBLIC_DEV 平行定义；无有效执行便不计模型语义失败。Round5.2真实 CLEAR→时间修改链与本轮组件回归继续保留，但不冒充新平行切片的Live覆盖。

有效 F 纠正 proposal 为 REPLACE/正确既有Task；J为 NEW_TASK，未被Pending劫持。两者均在下游安全拒绝，未发布新的当前计划。已接受 F 前置的 Binding、TaskState、IR/Plan、Scope 一致；唯一接受计划不等于全场景 Safety 通过。其余 Wrong Inheritance、Historical Return、CLEAR/REMOVE Resurrection、True Ambiguity、Self-contained New Task 缺少本轮有效Live分母。3条损坏输入的 UNRESOLVED 不作为 Context bug。

## 验证、性能与边界

按 Consumer → Grounding → Catalog Time → 原Root recorded → Oracle → 受影响V2 → Context → Critical → Target 的顺序验证，Schema复审后补相关回归。Agent 最终 **563 passed /0 failed**（526既有 +37新增；去重合并最后的输入入口测试），Oagnet **261 passed /0 failed**（248既有 +13新增）。Context **8/8、18轮**；Critical **160/160**；Round3 Target **15/15**，13条Private仅机器回归，没有人工打开调参或Blind访问。选中范围 old-pass→new-fail **0**，collection errors **0**；不推断未重跑全量。Agent全量3266/27仅为历史基线。

初次新测试有2个fixture导入错误，诊断脚本有一次临时缩进错误，均在本轮修复并保留开发回执；未改旧断言。评测输入编码错误另行记录，不能用最终测试全绿掩盖无效Live调用。

| 性能单位 | Round5.2 n=5 | 本轮有效输入 n=3 | 本轮全部尝试 n=6 |
|---|---:|---:|---:|
| 模型calls/request | 2.20 | 2.67 | 1.83 |
| 输入token均值 | 27831 | 26721 | 15160 |
| 输出token均值 | 882.20 | 889.33 | 559.17 |
| 请求mean/p95秒 | 13.35/19.83 | 15.69/17.22 | 10.52/17.22 |

不同小样本不可宣称提速。全部尝试中的低均值受损坏输入提前拒绝影响。正常模型步骤未增加；高基数路径复用既有 source-value-choice 步骤。F新增一次定向检索及一次finish复验；模型验证使用冻结源适配器，不是生产SQL延迟。最终两次真实定向源读取分别约0.312/0.219秒，均通过索引访问检查且观察hash不变；原Probe的两次只读计时约0.265/0.219秒，仍因高基数返回不完整、候选为空。这些独立源读取与冻结适配耗时不能当成生产请求基准。详见 [performance_receipt.json](performance_receipt.json)。

最后 Source Schema 已变化，因此新旧录制输出采用版本化回归，第二阶段输入差异明确保留，不冒充严格同请求模型重测。原始录制、无效输入、模型输出、源值及凭据均不进 Git。

新增源观察在模型调用前已独立采集并冻结。原生 Round5.1 input header 沿用旧基础 fixture hash，本轮实际扩展版本以各次 `round53_sources.json` 和独立源证据 hash 标识；不混称旧源快照的严格原请求回放。复审同时为后续调用入口补上开始前的实际源文件/跨服务代码 hash 冻结与结束复验，本轮没有回填伪造的开始回执。

## 门禁与停止

- QUESTION_COMPLETION_CONTRACT_GAP：OPEN，未插队实现。
- CONTEXT_ATTACHMENT_CORE_READY：PASS_ON_IMPLEMENTED_CONTRACT；本轮没有证明Context新缺陷。
- CONTEXT_FOLLOWUP_READY：**NOT_READY**；有效健康目录Live覆盖不足，源请求/过滤Producer问题仍在。
- READY_FOR_V2_READ_ONLY_E2E_SMOKE：**NO**。
- 最高剩余阻塞：先用已修复输入预检补有效Live覆盖，再审查 SourceValueRequest / StructuredFilterEdit 的通用生成合同；不重新深挖已关闭城市路径。

业务SQL执行0、生产状态/数据库写入0、Catalog写入0、索引重建0、Blind访问0。实际源值与元数据只读查询不混写为“无数据库访问”。V1仍是正式路由。没有E2E Smoke、Benchmark、Shadow、Canary、8088切换或下一Round；提交、push、Draft PR收尾后停止。
