# Round 5.12 — Limited Scalar Deployment Readiness Closure

**LIMITED_SCALAR_DEPLOYMENT_PREPARATION_PARTIAL**

**READY_FOR_LIMITED_SCALAR_DEPLOYMENT_APPROVAL = NO**

Round 5.11 的隔离标量链已收口成启动时可选择的候选 Runtime、稳定 Redis 生命周期和可审计的未知结果处置，但当前实际 Catalog collection 尚未初始化，生产 Redis 也没有持久化/恢复证明。因此候选代码与 Dry Run 交付完成，实际受限部署仍不能申请批准。V1、8088、页面、生产配置和正式用户状态均未改变。

## 恢复与版本边界

本任务从远程上下文压缩失败后恢复。恢复时 Git 分支为 `v2-limited-scalar-deployment-ready-20260910T164141Z`，HEAD 仍是 Round 5.11 基线 `6b3a6e91e06ee5897a44ca2fd5d28532ecb68a79` / Draft PR #70；版本仓干净，9个中断前文件只存在于开发目录，没有本轮 commit、push 或 PR，也没有 Round 5.12/pytest 子进程。恢复没有撤销或重写这些修改，检查点见 [recovery_checkpoint.md](recovery_checkpoint.md)。候选代码提交为 `f700b624536b448607e70c5ed54679e6e10c82a2`；堆叠 Draft PR 为 [#71](https://github.com/dreamyang-1/data/pull/71)，base/head已核对。最终证据提交按 [git_commit_manifest.json](git_commit_manifest.json) 的命令解析。

## 启动接线

默认 `Settings.runtime_mode` 为 `V1`。只有进程启动配置显式选择 `V2_LIMITED_SCALAR` 时，应用 lifespan 才构建候选 handler；请求、历史、Pending 和模型输出不能切换模式。缺 Redis、模型、81/[205]/58 Scope、Catalog/vector/target pin、Oagnet/SQL源码摘要或时间合同即拒绝启动/就绪，不静默走 V1。

候选运行链为：现有 `create_app` / 原 `/agent_chat` 与 `/agent_chat/stream` → `RawTurnPlanner` → 当前受治理 `CatalogPublication` → 原生 ASL2 lowering → pinned SQL translator → 显式 `LIVE_READ_ONLY` transport → 严格结果合同 → 稳定 V2 Redis envelope。SQL typed executor 由 Agent 进程加载 `sql-translator/sql_translator_prod.py`，不要求改在线 SQL HTTP 服务。本轮运行依赖不再来自 `.eval_private`、录制 Task 或人工 Dataset。启动和 readiness 不执行业务 SQL，也不写测试会话。

现有三个服务继续使用 2026-09-10 19:26:39 +08 启动的 PID：Agent 26572/8088、Oagnet 27968/8021、SQL Translator 26172/48000；没有 reload、停止、重启或接管端口。

## 支持范围

候选只接受完整通过当前 Scope/Pin、目录、source bundle、ASL2 lowering、时间、pinned SQL、只读快照和单行结果合同的 `SCALAR_AGGREGATE`。Round 5.11 实际证明的例子是省份与年度过滤的订单去重计数，以及含税销售额 Decimal 求和；这不认证全部指标、全部标量聚合或全部关系/过滤。

Grouped、Detail、Ranking、Dataset follow-up，以及任何 lowering、时间、字段、关系、来源或结果证明不完整的标量计划都在业务 SQL 前安全拒绝，不改题、不丢条件、不使用问题字符串白名单，也不回退 V1。完整清单见 [supported_capabilities.json](supported_capabilities.json)。

## Store、TTL 与并发

测试 namespace 仍要求 Round 5.11 隔离格式；候选部署使用显式、稳定且与 V1 不重叠的 `youo:data-analysis:v2-limited-scalar:<deployment-id>`。重启不生成 run id。格式版本为 `limited-scalar-session-v1`，不兼容版本直接拒绝，不自动迁移。

Task、Attempt、Result、Dataset 和 Proof 保存在同一原子 envelope。默认会话 TTL 为24小时、message guard为7天，成功/失败回执不会无限积累：每会话最多100条消息、4 MiB。阶段发布保持初始绝对会话 TTL，不因每轮写入滑动延期；guard 长于会话，envelope 到期后同一 message 仍返回 `EXECUTION_SESSION_EXPIRED_REUSE_REJECTED`，不会被当作新请求重跑。该保证只覆盖 guard 保留窗口，且仍依赖 Redis 数据可恢复。

语义 `state_version` 与 envelope `revision` 已分离：前者保持 Task reducer 合同，后者使 `SUBMISSION_ATTEMPTED`、`RESULT_RETURNED`、`RESULT_VALIDATED`、UNKNOWN和人工复核的每次更新都参与 CAS。陈旧阶段快照不能覆盖较新记录。

## RUNNING 与人工处置

部署记录区分 `PRE_SUBMISSION_RESERVED`、`SUBMISSION_ATTEMPTED`、`RESULT_RETURNED`、`RESULT_VALIDATED`、terminal receipt 和 `EXECUTION_OUTCOME_UNKNOWN`。成功重放返回原回执；同 message 不同内容在 JSON 和 SSE 首字节前都按既有409合同拒绝；RUNNING/UNKNOWN/REVIEW_REQUIRED不启动第二次 transport。超时、transport不明和执行后Redis发布竞争保留为未知或发布冲突，不宣称未执行或成功，也不宣称跨 SQL/Redis 严格恰好一次。

管理工具默认只读，必须以完整身份、Scope、conversation和message精确定位。唯一 mutation 是带显式授权、操作者和原因的 `require-review`，并受 revision CAS 保护。它不能重发 SQL、录入业务值或制造成功。

## 隔离 Redis 与验证

最终真实 Redis 运行 `r51220260911t0240` 使用专用稳定测试 namespace。两个独立 Python 进程完成 RUNNING保存与 REVIEW_REQUIRED恢复；随后验证陈旧 revision 被拒绝、会话键删除后message guard仍阻止重放、未来 envelope版本拒绝。验证只操作3个明确键：过程中精确删除2个，最终清理1个，剩余0；没有SCAN、通配删除、FLUSHDB或FLUSHALL。模型调用0、业务SQL 0、正式用户状态写入0。

最终专项31/31通过（包含于受影响回执）；API/Scope受影响集合239/239通过。候选源码冻结后一次Agent全量为 **3563 passed / 27 failed / 0 collection errors**；27个nodeid与Round 5.11完全相同，old-pass→new-fail=0，新增11项测试。SQL Translator源码与消费合同未变，复用400/0；Oagnet未改，复用690/8既有失败。旧test expectation修改0。详见 [test_delta.json](test_delta.json)。

## Dry Run、回滚与未关闭阻塞

候选检查、未来启用计划、回滚计划和source digest命令均以Dry Run执行，进程修改0。工具在本轮主动拒绝`--execute`；实际切换仍需后续用户批准和维护窗口。V1旧会话不会转成V2；V2不存在状态不表示已恢复V1历史。未来启用与回滚均要求新会话。回滚保留V2状态和当前有效密钥，不让V1解释V2期间的新Task。见 [deployment_runbook.md](deployment_runbook.md) 与 [rollback.md](rollback.md)。

实际只读依赖预检返回 `CATALOG_COLLECTION_NOT_INITIALIZED`，所以当前81/[205]候选无法构建合法生产Pin。现有Redis runtime可连接，但审计证据仍显示AOF关闭、RDB save为空、无副本且无volume/backup/restore演练回执；隔离生命周期测试不能代替生产恢复能力。这两项阻塞受限部署批准。当前 `.env` 仍是V1且未修改。

`QUESTION_COMPLETION_CONTRACT_GAP` 继续是用户可见限制：当前原响应没有可信的补全问题展示，本轮没有用短追问或与计划不一致的文本伪造它。C2地区粒度、更多QueryShape、通用Dataset、完整Context/Pending/歧义、生产Catalog发布与Redis恢复仍未关闭。

| Gate | 状态 |
|---|---|
| LIMITED_SCALAR_STARTUP_WIRING | PASS_CODE_AND_ISOLATED_TEST；actual Catalog blocked |
| STABLE_STORE_LIFECYCLE | PASS_CONTRACT_AND_ISOLATED_REDIS |
| UNKNOWN_OUTCOME_QUARANTINE | PASS_NO_AUTO_REEXECUTION |
| DEPLOYMENT_AND_ROLLBACK_DRY_RUN | PASS_NO_PROCESS_CHANGE |
| READY_FOR_LIMITED_SCALAR_DEPLOYMENT_APPROVAL | **NO** |
| CONTEXT_FOLLOWUP_READY | NOT_READY_FULL_GATE |
| INTERNAL_DEMO_SEMANTIC_READY | NO |
| READY_FOR_USER_APPROVAL | NO |
| QUESTION_COMPLETION_CONTRACT_GAP | OPEN_USER_VISIBLE_LIMITATION |

唯一下一行动：由现有目录发布链初始化并发布81/[205]的受治理 Catalog collection，同时补齐生产 Redis 持久化/恢复回执；之后重跑候选启动/readiness Dry Run，再判断是否可申请限定标量部署批准。本轮不执行下一行动。
