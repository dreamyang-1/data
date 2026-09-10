# Round 5.10 — Scalar Live Read-only E2E

**V2_SCALAR_LIVE_READ_ONLY_E2E_PARTIAL**

真实 V2 标量主链已经闭合：三条指定请求均经过实际 Recognition、原生 typed lowering、数据源 58 的真实只读 MySQL 执行、结果合同校验及隔离状态保存。Q1→Q2 使用同一会话中的真实 Q1 执行后状态，任务版本从 1 变为 2，并重新查询 2026 年数据，没有复用 Q1 结果。最终状态仍为 PARTIAL，因为独立核对发生了 4 次尝试，超过上限 2；其中前两次被本地参数合同在连接前拒绝，后两次实际执行并通过内存断言，但结果摘要未在后续序列化错误前持久化。因此正式结论保留 `BUSINESS_VALUE_INDEPENDENTLY_UNVERIFIED`。

## 基线、范围和运行隔离

基线为 `6bd9480f03d8e48f9f079847b63c3e2e89480281` / Draft PR [#68](https://github.com/dreamyang-1/data/pull/68)。2026-09-10 14:04 UTC 重新核对 PR 为 OPEN、DRAFT、未合并，远程 head 与本地基线一致。本轮分支为 `v2-scalar-live-e2e-20260910t133335z`，堆叠到 PR #68 分支。

授权范围固定为 semantic model 81、business domain `[205]`、data source 58。Runner 是不监听端口的独立进程，显式加载当前 Agent 和 SQL Translator 源码；状态仅写入进程内 `LIVE_READ_ONLY` Store。8088、8021、48000 三个既有进程没有停止、重启或加载本轮代码。V1 仍是正式路由，公共 API/SSE/UI 均未修改。

目录和源值采用 frozen certified snapshot；本轮开始时重新核对 current snapshot digest 与冻结快照相同，并直接从当前 Redis 精确解析数据源 58。Candidate/source value 使用冻结证据，不代表当前实时向量检索质量。Catalog version 为 `3f9589b95d376ebdf9683425dcf6c646f2394b278718b033527bd5e08b64fe21`。

## 只读前置与 Transport

前置检查在任何业务 SELECT 之前完成。数据源 58 的模型归属、配置完整性、MySQL 版本族、会话超时能力和一致性快照均通过。实际源库的8个必需字段全部存在；相关索引元数据可读，`sales_order` 估算约59,019行，因此允许原样执行无时间 Q0。累计元数据/源检查6次，业务查询0次，事务均回滚并关闭。

SQL Translator 新增显式 `execution_timeout_ms`。只有隔离调用传入该参数时才在本次连接设置 MySQL `MAX_EXECUTION_TIME=30000`，并设置35秒驱动读取超时；默认公共调用不变。超时值必须是1–90,000的严格整数，设置失败则在业务查询前 fail closed。严格只读顺序为会话超时 → `SET TRANSACTION READ ONLY` → `START TRANSACTION WITH CONSISTENT SNAPSHOT` → snapshot clock → 单条参数化 SELECT。提交状态、超时未知、连接关闭和不可重试错误均有独立测试。

Agent Adapter 新增显式 `LIVE_READ_ONLY` provenance、30秒执行参数、结果端 timeout proof、执行后状态重新封装，以及与 TEST_ONLY 区分的 attempt/dataset namespace。仍无默认 Transport、生产 Store、HTTP 路由或生产持久化；未知 provenance、Scope/Pin不一致、State version不一致、结果不完整和缺 timeout proof 均在发布成功前拒绝。

## 三条真实标量链路

| Query | Recognition / Plan | SQL语义 | 执行 | 结果合同 | 数据库耗时 |
|---|---|---|---|---|---:|
| Q0 `查询含税销售总额` | PASS | `SUM(sales_order.amount_with_tax)`，无新增时间/过滤 | SUCCEEDED | 1行、numeric float、未截断、snapshot PASS | 0.328s |
| Q1 `查询去年江苏省订单笔数` | PASS | `COUNT(DISTINCT sales_order.order_key)`；省份连接；2025 `[start,end)` | SUCCEEDED | 1行、integer、未截断、snapshot PASS | 0.141s |
| Q2 `换今年` | PASS / MODIFY同一任务 | 保留订单笔数和江苏省，只把时间改为2026 `[start,end)` | SUCCEEDED | 1行、integer、未截断、snapshot PASS | 0.094s |

三个结果均具有请求、Prepared Plan、SQL模板、参数、Scope/Pin、snapshot和结果 digest；业务值和原始 SQL 仅保存在 PRIVATE。Q1/Q2 的时间按用户确认的 `Asia/Shanghai LOCAL_WALL_DATETIME` 生成左闭右开边界。历史迁移分界仍未知，本轮没有扩大该声明适用范围。

结果校验区分空结果、NULL和数值0，COUNT保持整数；没有 download、preview或截断。Q0 沿用原执行器把数据库 Decimal 转成 JSON numeric float 的格式，虽然接口可序列化，但精确货币小数是否有精度损失尚未证明，因此 `SCALAR_RESULT_CONTRACT` 不能宣称完整通过。

## 独立业务值核对

第一版独立核对脚本使用了 `region/start/end` 参数名，被既有 Bound SQL 合同在连接前拒绝；0次数据库提交。修正为 `v2_p0..2` 后执行了2025、2026两条独立的 INNER JOIN + COUNT DISTINCT 查询。两条结果均通过“执行成功、已提交、与对应主结果相等”的内存断言，之后脚本因把冻结 attempt 当字典而中止，未把独立结果和值摘要写盘。

所有失败尝试必须计数，所以独立核对为4次尝试、2次数据库提交，违反最多2次尝试的门禁。没有再次查询或用主查询伪造独立摘要。正式状态为 `BUSINESS_VALUE_INDEPENDENTLY_UNVERIFIED`，并保留“控制流断言已通过但持久化证据不足”的限制。

## 多轮状态与失败安全

Q1 成功 attempt 和 Dataset 先写入隔离 ConversationState，再由 `ScopedArtifact` 合同重新封装后送入 Q2。Q2 输出保留 Q1 attempt/Dataset，新增自己的 attempt/Dataset，task `last_dataset_id` 指向 Q2；只有一个任务，其他任务未发生变化。Q1/Q2 的参数和 SQL身份不同，证明 Q2 实际重新查询。7项状态检查全部通过。

幂等检查以相同 message ID 再调用 Adapter，直接返回既有 receipt，Transport调用仍为1。离线负例覆盖：超时/异常/坏结果不发布成功，旧 Dataset保持；并发较新状态不被覆盖；运行中重复请求不二次提交；Scope/Pin、Plan、参数、State version或Task version不一致均不提交。生产 Redis 持久化、跨进程 Dataset restore 和平台连续请求不在本轮证明范围。

## 调用、安全与性能

- 模型请求6次，qwen3.7-max、Thinking=false、temperature=0、retry=0；无超时或鉴权错误。
- 主业务 SELECT 3次尝试、3次提交、3次成功；独立核对4次尝试、2次提交；元数据/源检查6次。
- 每次真实数据库提交都使用独立连接、30秒服务端 SELECT 超时、严格只读一致性快照；无自动重试。
- SQL写入0、生产状态写入0、Catalog写入0、配置修改0、服务重启0、Blind访问0。
- Q0/Q1/Q2 单次数据库阶段分别为0.328s、0.141s、0.094s。它们是三次小样本观察，不是生产SLA。

恢复后的 `.env` 和模型配置未改，未公开凭据、主机、库名或业务值。Oagnet 工作区原有大量未提交改动和日志，本轮只读核对后保持不变，没有复制或提交其中任何内容。

## 回归

受影响 Agent 集合 **266 passed / 0 failed / 0 collection errors**。候选源码冻结后，Agent 累计回归 **3532 passed / 27既有failed / 0 collection errors**；相对上轮正式基线无 old-pass→new-fail。新增 Agent测试7项。SQL Translator 最终全量 **396 passed / 0 failed / 0 collection errors**，新增7项。Oagnet 源码未改，复用上轮 **690 passed / 8既有failed**，不声称本轮重跑。

测试进程使用既有离线 runner，没有继承真实执行 opt-in，也没有连接业务数据库。实际模型和数据库调用只发生在显式带 `--allow-model-calls --allow-read-only-source-58` 的私有 Runner 中。

## 门禁与下一步

| Gate | 状态 |
|---|---|
| SCALAR_LIVE_READ_ONLY_E2E | **PASS_MAIN_PATH** |
| SCALAR_RESULT_CONTRACT | **PARTIAL**；货币Decimal精度和独立值持久化证据未关闭 |
| SCALAR_STATE_CONTINUITY | **PASS_ISOLATED_IN_MEMORY** |
| READY_FOR_INTERNAL_DEMO_E2E_SMOKE | **PARTIAL_SCALAR_PATH_ONLY** |
| CONTEXT_FOLLOWUP_READY | **NOT_READY_FULL_GATE** |
| INTERNAL_DEMO_SEMANTIC_READY | **NO** |
| READY_FOR_USER_APPROVAL | **NO** |

公共平台接入仍缺真实会话持久化、执行/Dataset回执桥接、原API/SSE映射、仅支持能力的拒绝处理、`completed_question`合同、平台连续请求与回滚验证。已有 C2 地区口径、其他语义门禁、Dataset/DryPlan通用能力、生产Catalog发布和Redis恢复证据继续保留。下一阶段若继续，应先确定货币 Decimal 的兼容输出合同，并以新预算重新获取可持久化的独立值验证；本轮不启动这些工作。

本轮没有运行内部平台Smoke、Benchmark、Blind、Shadow、Canary或切流。完成一个独立提交和堆叠 Draft PR 后停止，不自动合并。

机器可核验证据、PRIVATE路径和hash见 [evidence_index.json](evidence_index.json)。
