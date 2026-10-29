# Round 5.11 — Scalar Result Contract & Persisted API Vertical Closure

**SCALAR_RESULT_AND_PERSISTED_API_CLOSURE_COMPLETE**

本轮限定标量链路已经完成：真实用户请求通过现有 `/agent_chat` 与 `/agent_chat/stream`，进入显式注入的 V2 Runtime，执行数据源58的只读查询，把精确结果、Task、Attempt、Dataset 和证明写入本轮隔离 Redis；新进程从 Redis 恢复同一会话后正确处理“换今年”并重新查询。V1、8088、页面、生产配置和正式用户状态均未改变。本结论只支持限定标量路径进入部署与回滚审查，不表示 V2 已上线或具备全功能替代 V1 的资格。

## 基线与修改范围

基线为 `dd1e5a766f5fb4c73bc6376bf0e284a179194531` / Draft PR [#69](https://github.com/dreamyang-1/data/pull/69)，开始时为 OPEN、DRAFT、未合并，远程 head 与本地一致。本轮分支为 `v2-scalar-persisted-api-20260910T230446Z`，堆叠到 Round 5.10 分支。

修改分为四个边界：SQL Translator 增加只供显式 typed execution 使用的 Decimal 保真开关；V2 execution 保留 Decimal 并用 Decimal 原生有限性检查；新增隔离 Redis 的 V2 Artifact/CAS 桥接和标量 API handler；应用工厂在显式注入时让原路由调用该 handler。默认应用不注入 handler，仍走原 V1 workflow。没有新增路由、请求字段、响应字段或 SSE 类型；没有修改 Prompt、模型 Schema、Catalog、业务语义规则或页面。

现有 `RedisSessionStore` 只直接管理 V1 Pending、Canonical Request、响应及引用，不能原生恢复 V2 `ConversationState` / `ScopedArtifact`。本轮桥接复用既有 V2 State、TaskVersion、ExecutionAttempt、Dataset、Proof、Scope/Pin 及 Redis CAS 版本语义，并把它们放入一个隔离、版本化 envelope；没有引入第二套 Task reducer 或改变生产 Store。该桥接没有生产配置入口，部署评审前仍需决定如何纳入正式 Store 生命周期。

## 调用链与精确结果合同

| 边界 | 本轮行为 |
|---|---|
| 原 API / 身份 / Scope | 继续使用 `ChatRequest`、可信身份校验和 81/[205]；缺失或伪造 Scope 的既有校验不绕过 |
| V2 Recognition / Plan | 使用当前 RawTurnPlanner、冻结 Catalog/Source Value、原生 typed lowering；仅接受 `SCALAR_AGGREGATE` |
| SQL 执行 | `LIVE_READ_ONLY` 请求显式传 `preserve_decimal=True`；公共 V1/HTTP 默认仍为 `False` |
| 结果校验 | COUNT 保留 `int`；金额保留 `Decimal`；NULL、0、空结果和非有限值分别处理 |
| 规范编码 / digest | `typed-scalar-result-v1` 明确编码 INTEGER、DECIMAL、APPROXIMATE_FLOAT、NULL 和时间类型；digest 基于带类型的稳定 payload |
| Redis 发布 | 先发布 RUNNING，再验证结果、Proof 和精确 Result Artifact，最后用 state-version CAS 原子发布 SUCCEEDED 与 Dataset 指针 |
| API / SSE | 值只进入现有 `answer: string`，结果证明进入既有 `evidence`；不新增数值字段或改变字段类型 |

数据库 Decimal 在旧执行器的 `_convert_types` 处曾转换为 float。现在只有显式 `LIVE_READ_ONLY` typed request 保留 Decimal，之后的校验、digest、Redis 保存和恢复都不经过 float。金额显示使用 Decimal 的精确十进制文本，不自行固定两位小数或增加舍入；内部原值不因展示改变。普通 float 继续标记为 `APPROXIMATE_FLOAT`。高精度小数、大整数、负值、零、NULL、超出二进制浮点精确范围的数、非有限值、恢复类型与 digest 稳定性均有离线测试。

## Redis 隔离与跨进程验收

run id 为 `r51120260910t230900z`，prefix 为 `youo:data-analysis:v2:isolated:round5-11:<run_id>`。启动时验证 prefix 必须包含本轮 marker 和 run id，TTL 为7200秒；缺少明确命名空间直接拒绝。实际写入与生产 prefix 不重合，State key 同时绑定 tenant、user、application、conversation 和完整 Authorized Scope fingerprint。

P1 和 P2 由两个独立 Python 进程、两个独立应用实例和 Redis client 执行。进程B只读取 Redis，没有接收进程A的内存对象。P1/P2 的同一会话最终 state version 为6，只有一个 Task，TaskVersion 从1到2；保留2个 Attempt、2个 Dataset，最新 Dataset 指向 P2，P1回执仍存在。重复 P2 返回原成功回执，模型、TaskPatch、Transport 和 SQL预算增量均为0。

本轮产生的6个 Redis key 均写入精确资源清单。验收结束后仅按该清单删除：删除前6/6存在且均有正 TTL，删除6，删除后剩余0；scan、FLUSHDB、FLUSHALL和通配删除均为0。真实连接信息、凭据和业务结果没有进入 Git。

## 真实请求与独立核验

| Case | 原接口 | 结果 | 持久化 / 独立核验 |
|---|---|---|---|
| P1 `查询去年江苏省订单笔数` | `/agent_chat` | PASS | 2025、省份、COUNT DISTINCT；int；Task/Attempt/Dataset/Result/Proof 持久化；同一只读事务内独立公式精确相等 |
| P2 `换今年` | `/agent_chat/stream` | PASS | 从 P1 Redis 恢复，保留指标和江苏省，只改2026；重新执行；SSE 为既有事件序列且恰好一个 complete、零 error；独立公式精确相等 |
| P2 同 message 重放 | `/agent_chat` | PASS | 返回原回执；模型和 SQL 调用均未增加，State/Task未重复应用 |
| `查询含税销售总额` | `/agent_chat` | PASS | 数据库 Decimal 到 Redis 恢复仍为 Decimal；同一只读事务内独立 SUM 精确相等；对外使用现有 answer 字符串 |
| 不支持的 grouped shape | `/agent_chat` | PASS_SAFE_REJECT | 受控计划在 capability gate 返回 `EXECUTION_PAYLOAD_UNSUPPORTED`；模型、Transport、SQL均为0；没有改成标量或回退 V1 |

三组新独立核验均在主查询与独立编写的参考查询共用的真实连接、只读事务和一致性快照中完成；每组回执在事务结束时立即写入 PRIVATE，汇总只读取已保存回执。Round 5.10 的4次旧尝试和 `BUSINESS_VALUE_INDEPENDENTLY_UNVERIFIED` 原样保留；新证据版本为 `PASS_SAME_TRANSACTION_SNAPSHOT`，没有反写旧结论，也不等同财务报表对账。

金额流程前两次复核被 runner 的无参数 Bound SQL 本地适配错误拒绝，均在数据库连接前失败，并持久化 FAILED、零 Dataset。修正仅发生在评测工具；最终第三次金额流程通过。所有尝试都计入预算，没有只统计数据库提交。

## 一致性、失败和恢复边界

成功发布顺序为 Result/Proof 完整验证后再 CAS 提交 terminal state；SUCCEEDED、精确 Result Artifact、Attempt 和 Dataset 指针位于同一 Redis envelope 中。失败和超时保存 FAILED Attempt、SAFE_FALLBACK 及空 Result，不更新成功 Dataset。旧 state version 的 CAS 失败不会覆盖较新状态；执行已经提交但 Redis 发布冲突时返回 `EXECUTION_RECEIPT_STATE_CONFLICT`，不声称未执行或成功。相同 message 的不同请求返回409冲突。

RUNNING 重复请求返回 `EXECUTION_OUTCOME_PENDING_REVIEW`，不启动第二次 Transport。进程崩溃后的未知结果保持隔离和禁止重执行，等待人工/后续运维核对或 TTL 到期；本轮没有声称跨 SQL 与 Redis 是严格恰好一次，也没有扩建调度恢复系统。该恢复运维流程仍是正式部署评审项。

离线故障注入覆盖 transport timeout、坏结果、Scope/Pin/计划/版本不一致、保存竞争、同 message 冲突、RUNNING 重复、失败不生成 Dataset、已成功请求不重新执行、旧版本不覆盖新状态。当前标量链没有 CLEAR/REMOVE 操作；相关防复活合同继续由既有 Critical 回归约束，本轮不把它计作真实 API 链覆盖。

## 调用预算、性能和回归

- 模型请求10次，目标8以内未达到，硬上限12未超过；全部使用 qwen3.7-max、Thinking=false、temperature=0、retry=0。
- SQL尝试10次：数据库提交6次、成功6次、失败0、未知0；另外4次在连接前被本地参数合同拒绝。硬上限12未超过。
- 元数据/数据源请求6次，上限8；SQL写入、Catalog写入、生产状态写入、配置修改、服务重启、Blind访问均为0。
- P1、P2、最终金额原 API 总耗时分别约22.531s、11.953s、11.500s；对应主查询加独立核验的数据库阶段约0.094s、0.125s、0.156s。它们只是小样本观察，不是生产 SLA。
- 最终受影响测试 **131 passed / 0 failed / 0 collection errors**。
- Agent 累计 **3552 passed / 27既有failed / 0 collection errors**；27个失败 nodeid 与 Round 5.10 完全相同，old-pass→new-fail=0，新增20项，旧测试 expectation 修改0。
- SQL Translator **400 passed / 0 failed / 0 collection errors**，新增4项。
- Oagnet 源码未改，复用 **690 passed / 8既有failed**，不声称本轮重跑。

现有三个服务仍为原 PID、原启动时间并监听8088/8021/48000；本轮没有重启或部署候选代码。隔离 API 使用真实应用工厂、认证、路由和响应模型，但属于进程内 ASGI 验证，不能称为浏览器平台或生产接口已切换。

## 门禁与停止点

| Gate | 状态 |
|---|---|
| SCALAR_EXACT_VALUE_CONTRACT | **PASS_LIMITED_SCALAR_PATH**；COUNT=int，金额=Decimal内部保真，现有 answer 字符串兼容 |
| BUSINESS_VALUE_INDEPENDENT_VERIFICATION | **PASS_NEW_ROUND511_SAME_SNAPSHOT**；旧 Round5.10 未验证记录保留 |
| SCALAR_PERSISTED_STATE_CONTINUITY | **PASS_ISOLATED_REDIS_CROSS_PROCESS** |
| SCALAR_API_SSE_INTEGRATION | **PASS_ISOLATED_ORIGINAL_ROUTES** |
| UNSUPPORTED_CAPABILITY_HANDLING | **PASS_PRE_SQL_SAFE_REJECT** |
| SCALAR_INTERNAL_PLATFORM_CANDIDATE | **READY_FOR_DEPLOYMENT_REVIEW** |
| CONTEXT_FOLLOWUP_READY | **NOT_READY_FULL_GATE** |
| INTERNAL_DEMO_SEMANTIC_READY | **NO** |
| QUESTION_COMPLETION_CONTRACT_GAP | **OPEN_UNCHANGED**；当前响应模型和页面闭环不要求该字段 |
| READY_FOR_USER_APPROVAL | **NO** |

下一项唯一行动是对该限定标量 Adapter 做正式部署/回滚设计审查，明确 V2 Store 与生产 Redis 生命周期、RUNNING 未知结果运维处理以及启用配置；在此之前不接8088。C2地区粒度、其他 QueryShape、通用 Dataset/DryPlan、生产 Catalog 发布、Redis恢复能力和全局语义门禁继续保留。

本轮完成一个独立提交和堆叠 Draft PR 后停止，不自动合并，不开始下一阶段。机器证据、PRIVATE hash 和文件清单见 [evidence_index.json](evidence_index.json)，回滚见 [rollback.md](rollback.md)，隔离资源说明见 [isolation_resources.md](isolation_resources.md)。
