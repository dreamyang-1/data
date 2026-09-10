# Round 5 — Context Proposal Production Wiring

**CONTEXT_PROPOSAL_PRODUCTION_WIRING_PARTIAL**

V2 RawTurnPlanner 已正式使用 bounded candidates + 同次 LLM relation/target proposal + hard validation；功能与安全接入已完成。**性能门禁仍缺旧生产链匹配的 Recognition/整体 V2 延迟回执，因此不标 COMPLETE。** 本轮20次模型预算已用完，不追加调用补造结论。V1 继续正式路由，未启停或接管8088；提交及 Draft PR 后停止。

Baseline：`899b7b692f7ff06a8660fe99361ba39f8528b1f1`，父 Draft PR #58 已重新核对 OPEN/DRAFT/未合并。分支 `semantic-context-production-wiring-round5-20260910t015554z`。开发先在 `E:/YouoAgent/DataAnalysis_Agent`，按 manifest 同步 `E:/yy`。开始1541个 tracked file 无意外 drift。最终提交由 `git_commit_manifest.json` 的 introducing-commit 命令解析，发布回执保留在提交外，避免递归写自己的SHA。

## 接入与 Schema

生产修改仅4个文件：`context_contract.py`、`context_proposal.py`、`recognition.py`、`catalog_bridge.py`。候选发现位于 `discover_context`，在首次模型调用之前，最多4个任务/24000字符；输入不含完整历史、SQL或结果行。当前可信 request/scope 是唯一授权源；历史必须通过 native restore。

`ContextAwareParse.context_proposal` 同时声明关系、目标和状态。`accept_proposal`/`validate_proposal` 只校验 scope、候选存在性、版本、relation-target/Pending 一致性；不按缺 metric/time、句长、关键词或confidence否决模型。`proposal_resolution` 直接进入 Raw 主链；`ScopedPlanSession.resolve_turn` 重验冻结的 request-local proof，再登记编译证明。第二阶段只看选中任务，不能改选其他历史目标。TaskPatch/Reducer及当前显式输入保护不变。

R4L-011 的唯一 Schema Reject 属于 **C：实验 Schema 导出与 Python validator 合同不一致**。模型当时收到的 Schema 没有导出 ambiguous 时 relation 必须 null 的跨字段谓词。本轮导出相同谓词，不放松 validator、不新增语义 primitive。真实最终 Schema 重试得到 AMBIGUOUS/null relation/null target，并走到结构化 terminal；原拒绝不重算为旧版PASS。

AMBIGUOUS / UNRESOLVED 返回具有既有 `TerminalDecision` 的 `ContextProposalFailure`，明确状态/reason，不猜任务、不执行 Draft/Patch、不修改状态；未新建面向用户的 Task-choice UI/Pending协议。Pending 答案仍要求真实 resume receipt 和合法选项；没有回执不会阻止完整 NEW_TASK。当前上下文 arbitration trace 只记录结构化ID/版本/Scope/约束/reason，不记录敏感全文。

PARSE/DRAFT Prompt 字面文本修改0；新增 Schema字段说明7句，属于有效模型输入变更，明确披露。生产语义 Regex、业务关键词特判、confidence阈值新增均0。正式 parse/resolver 版本已写入现有 plan version metadata。

## 验证与证据边界

| 项目 | 结果 |
|---|---|
| Final Schema / 原12条公开对照 | 12/12 合法 proposal；11个选中目标正确 + 1个明确歧义不选目标；relation checks 12/12 |
| 原生后续计划 | 10 PLAN、1 AMBIGUOUS TERMINAL、1既有 S81-016 Slot Operation安全拒绝；不是 Whole Plan准确率 |
| 实际模型请求 | 20：12首次 + 6同轮下游 + 2复用同一首次输出的下游补采；没有额外 context LLM |
| 混合记录 | S81-012/016下游复用Round4记录，明确不算当前模型完整路径；两条初始mention-ID不兼容记录保留，后续仅补采对应Draft |
| Final-runtime严格回放 | 12/12输入/Schema/Prompt匹配，语义payload/patch/state及拒绝一致；0模型/SQL/生产写入 |
| 原Round3 Target簇 | 15/15目标保留；13条原安全拒绝原因不变，2条接受结果语义等价；协议迁移回放，非新模型15/15 |
| Context Critical Slice | 8/8，18 turns，包含ADD/REMOVE/CLEAR后续屏障、历史返回和Pending新任务 |
| Critical | 160/160 |
| 新增测试 | 25项；冻结证明、状态漂移、Scope/未曝光目标、Pending对照、歧义/未解析等 |
| 全量及最终受影响节点复验 | **3266 passed / 27既有 failed**；131模块；collection errors0；old-pass→new-fail0 |

本轮全量采用既有断网/禁用dotenv/固定时钟 runner 分批执行，再对变更节点精确复验；不是把旧3183基线机械加新增数。旧Round1实际全量3183/27、Round4专项322/0均保留来源。测试和fixture迁移见 `test_contract_migrations.md`，评分期待未按模型结果改写。Oagnet/SQL源码未改、未重跑其全量。

全部10个计划的已声明可观察标签轴未发现错误；S81-014 `time_relation` 仍无单独观察，未标轴不推算为正确。没有重新评测全部Gold，没有把这组相关公开控制当总体准确率。13个Private capture仅在断网脚本内作原目标非退化检查，未人工读取原句/逐Case调参，未新增promotion；Blind访问0。

## 性能

| 指标 | Before | After |
|---|---:|---:|
| 正常 / 普通Context LLM calls/request | 2 / 2 | 2 / 2 |
| 精确Pending答案 calls/request | 1 | 1 |
| 匹配5条案例，两阶段输入token均值 | 16162 | 17086（+5.72%） |
| 匹配5条案例，两阶段输出token均值 | 558.8 | 621.0 |
| 首次Recognition延迟 mean/p95 | UNKNOWN：原生产计时缺失 | 8.505 / 10.077秒，n12 |
| 完整live V2延迟 mean/p95 | UNKNOWN：原生产计时缺失 | 12.649 / 15.446秒，n6 |
| 本轮timeout / Schema reject | 不作跨批推算 | 0 / 0 |

Token来自Provider usage；增长来自bounded context与联合Schema，不是新增模型调用。Round4实验joint-proposal计时、离线context discovery/validation与legacy seam计时另列 `performance_receipt.json`，不冒充旧生产端到端基线，也不把不同时段/不同工作量直接当因果性能对比。Seed/top_p/max_tokens未发送，不能宣称确定性。**延迟非退化尚不可证明；性能Before/After门禁PARTIAL。**

## Gate与停止点

17项接入条件中16项PASS，性能条件PARTIAL。`CONTEXT_ATTACHMENT_CORE_READY=PASS_ON_IMPLEMENTED_CONTRACT`；`CONTEXT_FOLLOWUP_READY=NOT_READY`。当前轮引用/显式slot声明的12条安全拒绝、既有S81-016 slot冲突，以及前阶段Full Plan/Dataset/DryPlan/完整对照门禁仍在；没有因这次接入把它们宣布修好。

当前最高Round5缺口是匹配的旧生产延迟证据。进一步真实配对需要新的模型预算，本轮停止调用。不能进入V2接管8088准备；`READY_FOR_USER_APPROVAL=NO`。UI/API/SSE、V1、Reducer、生产身份系统和跨服务代码修改均0。没有Blind、正式Benchmark、Shadow、Canary或切换。下一轮必须单独确认目标，本轮只发布可审查的接入成果和剩余证据缺口。
