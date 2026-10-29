# Round 5.5 — Source Value / Filter Target Alignment

**ROUND_5_5_FIELD_ALIGNMENT_PARTIAL**

已有 Filter Target 的字段身份现在会在 Source Value 检索前被校验，生成 view 同时导出已有目标选择约束。原失败地区替换已获得真实 Live 成功证据；但新首问和新话题仍有两个更早的模型输出分歧，**INTERNAL_DEMO_SEMANTIC_READY = NO**，**READY_FOR_INTERNAL_DEMO_E2E_SMOKE = NO**。本轮停止，不执行 E2E、SQL 或下一 Round。

## Git / 基线

起始 HEAD 为 `67f2c8022d1014ee4f6dc3af17f677d12baa93ca`，PR #63 OPEN/DRAFT/未合并，base 为 #62。开始时 1641 个 tracked file 与 `E:/YouoAgent/DataAnalysis_Agent` 一致，`E:/yy` 工作树干净。独立分支为 `semantic-field-alignment-round5-5-20260910t072433z`，Draft PR 堆叠到 #63，不 merge。最终 SHA 由 `git_commit_manifest.json` 的 introducing-commit 命令精确解析，提交后验证 Git blob、开发目录、镜像及远端 HEAD。

## Root / Oracle

原始 First Divergence 是 SourceValueRequest 的字段选择：当前 Filter 为 `province.province_name`，模型另选 `city.city_name`，而两个字段在目录中不是等价身份。分类 **C / F**：允许不兼容字段选择，以及生成 Schema 未导出现有 target-selector 约束。不是 Context、Producer 丢 Target、Adapter 丢字段或 Consumer Missing。

最小改动为动态生成 view + 检索前 canonical field identity 检查。保留现有相同字段 fields-selector 的 Runtime 兼容性；保留最终字段校验。所有新增生产 Regex、业务关键词、confidence 常数和 Prompt 文本规则均为0。

严格保留原 city Candidate 的 Oracle **仍拒绝**，未声称满足 `SOURCE_VALUE_FILTER_ALIGNMENT_CAUSAL_ROOT_CONFIRMED`。仅纠正 SourceValueRequest selector 后，由原冻结观察重新取得 province Candidate 的对照通过真实 State/IR/Plan；候选身份因此改变，不能冒充同 Candidate 实验。修改后的原 city 输出也会在检索前拒绝。实际 Live A 的模型自主选择正确 target 后通过，另行计数。完整链与实验边界见 [current_source_value_filter_alignment_path.md](current_source_value_filter_alignment_path.md) 和 [root_cause_receipt.json](root_cause_receipt.json)。

合同校验已落实，但未声称所有字段选择失败都已消失。下一分歧是 **TASK_OPERATION / metrics SET 对象而非列表**，以及 **TURN_RESOLUTION / 完整新任务被模型判断为 MODIFY**。本轮未据此追加生产补丁。

## Catalog / Healthy Fixture

重新只读获取 81/[205] 的实际目录，版本与冻结快照一致。订单笔数：`COUNT(DISTINCT sales_order.order_key)`；销售总数量：`SUM(sales_order.quantity)`；两者都声明 `sales_order.created_date` 时间锚及物理映射。Source prerequisites 使用已认证、与该目录版本一致的只读冻结观察；当前 Live 不查询业务源数据库。

核对了订单→采购医院→省份的正式关系和 join key，以及地区 Attribute / Dimension 映射。此处只证明这条目录路径存在，**不认证所有地区归属路径、跨实体 SQL 可执行性或业务数据结果**。订单笔数的显式 bind_dimensions 未列 province，目录又存在经医院到省份的关系；本轮地区是 Attribute Filter 而非 province 分组，未把关系可达推导为所有 Metric–Dimension 组合均获认证。这一边界不能在后续 E2E 中被忽略。

销售额维持 `CATALOG_TIME_CONTRACT_GAP`，未猜时间。J 的医院→省份、医院→城市关系实际存在；原 Parse/Draft 选择 RELATION_LIST，却没有 relationship mention/edit，因角色未请求关系候选，offered relation candidates=0。最终分类 **MODEL_QUERY_SHAPE_RELATION_SELECTION_GAP**，不是已证明的目录关系缺失，也未证明 Engine Propagation Bug；到此停止该 Case。无 Catalog 写入、关系创建或字段等价猜测。

完整验证见 [catalog_healthy_fixture.json](catalog_healthy_fixture.json) / [catalog_boundary_audit.json](catalog_boundary_audit.json)。快速私有读取摘要中的顶层 formula 不是正式公式字段；正式公式证据来自实际 snapshot 的 `calculation_rule.calc_formula`，未将顶层 null 判为公式缺失。

## Preflight / Live Chains

工作目录、UTF-8、非空原句、fixture hash、Scope81/[205]、Catalog Pin、公式、时间锚、Field Mapping、Source prerequisite、Schema 与请求序列化通过。Agent Settings 在外部 fixture import 前固化，`.env` 未改。先执行一次最小 Provider Smoke，HTTP200；后续 continuation 复用该成功回执及未改变的配置证据。**401/403=0，invalid configuration request=0**。

本轮原始三条 fresh chain 共用一个真实新首问“查询去年江苏省订单笔数”。该轮模型把 metrics SET 填成对象，原生消费端要求列表，安全拒绝；三条链因此保留 BLOCKED_HISTORY。没有伪造成功历史，没有以 NOT_RUN 填充覆盖，也没有删掉这次失败。

为独立验证本轮目标，随后恢复先前真实成功的“查询去年江苏订单笔数”完整 receipt，复核 Scope、Pin、Task/Plan；仅共享这个原样执行前缀。以下 continuation 是新 Live 请求，旧首问不算本轮新执行轮，不能把它们改报成三条 fresh chain 全绿。

| Chain | 新 Live 轮 | 结果 |
|---|---|---|
| A | 换成北京市 → 不限地区 → 换今年 | 3/3 PASS：province Target 正确；REPLACE 仅剩北京市；整条件 REMOVE 原生降低为 CLEAR；随后时间改2026，地区未复活 |
| B | 再加销售总数量 → 不要订单笔数 → 换今年 | 3/3 PASS：ADD 保留两指标；删除第一个指标后仅剩销售总数量；下一轮无指标复活，江苏省仍保留 |
| C | 查询今年北京市销售总数量 → 返回原江苏订单任务 | 新任务轮 FAIL，返回轮未执行：模型判断 MODIFY 并选择旧 Task；随后 city/province 请求冲突被前置校验拒绝 |

六个接受计划逐轮独立检查了 expected Metric set、地区、年份、TargetTask、语义操作、TaskState/IR/LogicalPlan、CLEAR barrier、当前 Scope 和无原地状态变异。时间轮在 native Relation 中为 MODIFY，实际 temporal edit 为 REPLACE；没有改正式 expectation 为模糊“任意关系都通过”。完整结果见 [accepted_plan_checks.json](accepted_plan_checks.json)。

Optional Pending→New Task 和 True Ambiguity 未运行：三条核心链未齐。Self-contained New Task 已在 C 实际失败，不另找容易原句替换。

总计 **8个新 Runtime 请求，6个已声明轴 PASS、2个安全拒绝**；1个历史前缀仅复用。Primary fresh chains：0 PASS /3 BLOCKED_HISTORY；continuation：2完整链 PASS /1 FAIL。单位分开，不合成“成功率”。

## First Divergence / Safety

每个新失败只有一个第一分歧：新首问 **TASK_OPERATION**（SET operand list contract）；C **TURN_RESOLUTION**（完整新任务误判 MODIFY / wrong target proposal）。C 的 `SOURCE_VALUE_FILTER_FIELD_MISMATCH` 只记 downstream effect。后续未发布 State、IR、Plan，不额外统计为独立根因。

模型 Relation 错误 **1/8**，Target proposal 错误 **1/8**；没有规则覆盖该语义判断。Mention、Role 未发现本轮独立已证实主分歧，但未声明全部轴已认证。Operation 主分歧1；下游 Field Target / Grounding 冲突1且安全拒绝。六个已接受计划中，Binding、Patch、Reducer、State、IR、Plan 的独立声明轴未发现偏差。

**Wrong Silent Plan=0/6已检查接受计划；Scope Expansion=0/6；错误字段/源值接受=0；观察到 Catalog ID Fabrication=0**。CLEAR、REMOVE 各有一条实际后续轮，无复活。C 的错误 Target proposal 没有变为已发布错误计划或继承结果。这些数字不是全能力 Safety 或生产认证；Pending、完整 Historical Return 覆盖仍不足。

## Regression / 复审

受影响专项 **618 passed /0 failed**：604 个既有节点、14 个新增；old-pass→new-fail=0，collection errors=0。包含 Critical160、Context8条/18轮、SourceValue/Scope/Filter/Temporal/RawTurn/前置配置隔离与旧计划评测负控制。单独字段专项71项通过，属于上述集合，不重复加总。

Agent / Oagnet / SQL Translator **全量均 NOT_RUN**：用户要求先完成三条核心链并达到 Demo Gate，再运行三服务全量；本轮未满足。没有推算新的全量 passed/既有 failed，也不宣称未运行的三服务 regression 通过。Oagnet/SQL 源码未变。

开发期间3个新增测试最初将带 Prompt 的 system message 当 JSON 解析，修正为提取真实 JSON Schema；未改生产语义或旧测试 expectation 来消除该测试错误。新前置 Guard 的拒绝、原字段 selector 兼容、Target selector、初始条件、ADD、REMOVE 及不存在 target 的对比均在专项中验证。

## Performance

真实模型调用 **17/24**：1次 Provider Smoke +16次语义请求，全部HTTP200；8个 Runtime 请求均2次模型调用。Input tokens **197252**，output **6517**，含 Smoke、排除旧前缀历史成本。单模型阶段 mean/p95 **7.353/14.312秒**；两阶段 Recognition 合计 mean/p95 **14.705/22.250秒**；完整 Request mean/p95 **15.008/22.515秒**。

Provider timeout=0，Provider/parser Schema failure=0；另有1次 Runtime typed operand拒绝、1次生成合同未遵守后安全拒绝，不能因为HTTP200而写成语义全通过。Grounding生产检索延迟 **NOT_MEASURED**，Live使用冻结源值观察。不是 Benchmark，也没有匹配样本的提速声明。

## Gates / 停止

| Gate | 状态 |
|---|---|
| QUESTION_COMPLETION_CONTRACT_GAP | OPEN_UNCHANGED |
| CONTEXT_ATTACHMENT_CORE_READY | PASS_ON_IMPLEMENTED_CONTRACT |
| CONTEXT_FOLLOWUP_READY | NOT_READY |
| INTERNAL_DEMO_SEMANTIC_READY | NO |
| READY_FOR_INTERNAL_DEMO_E2E_SMOKE | NO |
| READY_FOR_V2_READ_ONLY_E2E_SMOKE | NO |

剩余不只是 Catalog/Business Gap：新任务的模型关系判断、metrics SET 生成表示及整链 Live 覆盖仍阻塞。正式门禁未降低。

Prompt文本、Context、Reducer、Grounding架构、业务关键词、权限框架、V1、UI、API/SSE、8088修改均 **NO**；Validator relaxed **NO**，新增的是检索前一致性校验；生成 Schema view changed **YES**，未新增 Runtime primitive。业务SQL执行0、生产写入0、Catalog写入0、Blind访问0。V1继续正式路由。提交并推送独立 Draft PR 后停止。
