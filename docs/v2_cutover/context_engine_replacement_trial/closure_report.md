# V2 Context Engine Replacement Trial

**V2_CONTEXT_ENGINE_REPLACEMENT_TRIAL_COMPLETE_LIMITED_SCALAR**

8088 内部测试环境已通过原 `/agent_chat` 与 `/agent_chat/stream` 接入现有 V2 理解桥接。调用链保持为原 API、身份与 Scope 校验 → V2 Recognition / Context Proposal / Task Resolution → 同一最终语义生成完整问题与 `CanonicalAnalysisRequest` → 原 Oagnet 查询适配器 → SQL Translator → 数据源58 → 原响应结构。没有增加路由、请求/响应字段、SSE 事件、Redis schema、Session 架构或第二套执行 Runtime；Oagnet 与 SQL Translator 源码未改。

提交默认仍是 `DATA_AGENT_RUNTIME_MODE=V1`。8088 当前仅通过本地未提交配置启用 `V2_LIMITED_SCALAR`，固定 Scope 81/[205]，可通过恢复试运行前 `.env` 并重启同一 uvicorn 入口回滚。为避免重建目录或向量索引，内部试运行新增显式 `LIVE_READ_ONLY_SNAPSHOT` 目录访问模式：只读捕获当前权威目录并在进程内提供精确候选，Pin/finish 会重新核对权威快照；它不写 Redis/Milvus，也不改变下游 Oagnet 的既有检索链。默认目录模式仍为 `PUBLISHED`。

## 原接口验收

| Case | 真实请求链 | 结果 |
|---|---|---|
| A | 去年江苏省订单笔数 → 换今年 | PASS；TaskVersion 1→2，保留地区/指标，只把时间改为2026；第二轮走原 SSE。 |
| B | 去年江苏省订单笔数 → 再加销售总数量 | PASS；最终指标为订单笔数和销售总数量。 |
| C | 订单笔数和销售数量 → 不要订单笔数 | PASS；目录同义词“销售数量”绑定正式指标“销售总数量”，最终只剩销售总数量。 |
| D | 已有江苏订单任务 → 查询北京医院数量 | PASS；创建 NEW_TASK，完整问题中没有江苏、订单或旧时间。 |
| E | 已有江苏订单与北京医院两个任务 → 返回刚才江苏订单 | PASS；选择原历史 Task，恢复江苏省、订单笔数和该 Task 的2026时间，没有修改北京任务。 |

Case E 的 first divergence 是：模型已正确给出 `RETURN_TO_TOPIC` 和合法目标，却把用于定位目标的“江苏、订单”再次声明为当前 slot，最终被 `V2_EXPLICIT_SLOT_DROPPED` 安全拒绝。最小修复只在以下条件全部成立时把 mention 归为历史任务描述：关系和目标已经通过硬约束；没有操作、否定、时间或 topic-shift 信号；所有显式 mention 都已存在于该目标 Task 的受保护语义摘要。任何新值、新指标、操作标记或不匹配目标的 mention 继续保留并由原严格编辑守卫处理。

Case C 的 first divergence 是模型选择了正确指标身份，却复制了同一身份为另一 mention 提供的 handle。修复只允许依据受治理目录中唯一、精确的 display name 或 synonym 重绑到当前 mention；不做模糊匹配、不创建目录身份，也不放宽 Scope、角色或当前轮证据校验。

“查询江苏订单”本身不能确定是订单笔数还是订单明细，真实试运行保持安全拒绝，没有硬编码“订单=订单笔数”。一次新话题模型输出出现下游 subject ownership 安全拒绝；同一已有状态的正式 D 验收随后通过，说明状态隔离合同可用，但小样本不证明模型输出确定性。

## SSE、Redis 与幂等

- A 的第一轮成功后重启 8088；第二轮从 Redis 恢复同一 Task，TaskVersion 从1变为2，并重新执行2026查询。
- E 首次走 JSON，随后以相同 conversation/message/question 走 SSE。两次的完整问题、answer、Dataset ID、Task Plan 和 Analysis Plan 完全一致。
- E 重放前后 Redis revision=20，TaskVersions=[2,1]，Attempt=4、Dataset=4、Message=4、Plan=2，全部未变化。因此重复 message 没有再次规划、调用模型或提交查询。
- 完整问题由本次执行消费的最终 TaskState/Canonical Request 生成，没有额外模型调用或独立展示语义。

## V1 对照边界

本轮在切换前对 V1 原 8088 做了一次带现有后端认证的真实请求；150 秒内未得到终态，无法形成共同完成分母。没有继续发起多次长时间 V1 调用，也没有把超时或旧手工服务串联结果伪造成 10–20 条可比准确率。V2 的 A–E 共11个声明轮次，其中复用已成功前置后有8个新原接口请求完成。当前证据支持限定上下文试运行，不支持 V1/V2 总体准确率排名。

## 验证与边界

- 受影响上下文/API/部署专项：322 passed / 0 failed。
- Critical：160 passed / 0 failed；首次运行因本地 8088 `.env` 使 V1 fixture 启动 V2 而出现9个环境启动失败，隔离测试子进程为 V1 后原分母全部通过。
- Agent 全量：3608 passed / 27 个既有 failed / 0 collection errors；失败仍为既有 Legacy 集合。
- 真实 8088 `/ready`：`READY`，runtime=`V2_LIMITED_SCALAR`；Oagnet 8021 与 SQL Translator 48000 原进程保持运行。
- API/SSE/UI 格式变化0；Oagnet/SQL Translator修改0；生产配置提交0；Catalog/向量写入0；数据库写入0；自动合并0。

当前结论限于平台首问、时间追问、指标 ADD/REMOVE、新话题隔离、历史返回和完整问题展示的标量链路。名单、排名、复杂分析和完整替代门禁仍不在本轮范围。8088 是内部试运行，不等于生产切流，也不是 `READY_FOR_USER_APPROVAL`。
