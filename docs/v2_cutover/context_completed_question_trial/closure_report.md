# Context and Completed Question API Validation

**CONTEXT_COMPLETED_QUESTION_API_VALIDATION_COMPLETE**

本检查只验证当前已有桥接，没有修改 8088 运行进程、Oagnet、SQL Translator、Redis schema 或 Session 架构。V1 继续保持正式路由。

| 验证项 | 结果 | 证据边界 |
|---|---|---|
| SSE 与 JSON 使用同一 Canonical Request | **PASS** | 同一 message 先经原 `/agent_chat`，再经原 `/agent_chat/stream`；SSE 返回持久化的同一 `analysis_process`，Canonical Query 入口只收到一份完全相同的 `CanonicalAnalysisRequest`。 |
| Redis 恢复保持 Task Version | **PASS** | 新建 Store 实例后从同一稳定 namespace 恢复 envelope；恢复前后 `Task.active_version` 与计划 `task_version` 一致，重复消息后版本不变。跨进程诊断也已恢复到 relation/target/version 校验；其后因录制模型输出携带旧动态 filter handle 而停止，这属于回放 fixture 的 handle 重绑定缺口，不是当前 Store/Task Version 缺口。 |
| 重复 message 不重复模型与查询 | **PASS** | 首次 JSON 请求后以同一 message 走 SSE；planner/模型入口调用 1 次，原查询 Adapter 调用 1 次。第二次直接复用持久化响应。 |

专项验证为 **4 passed / 0 failed**，最终受影响集合为 **148 passed / 0 failed**。一次 Agent 全量检查得到 3586 passed / 37 failed；其中 27 项与 Round 5.12 基线相同，新增 10 项已定位为 subject coverage 对既有 `source_entity` 的兼容遗漏及生成 Schema 快照未同步，最小修正后这 10 项逐项通过。没有据此重写既有 27 项失败或放宽测试期待。

固定的原接口真实链已完成 12/12 请求轮次，完整问题、TaskState、Canonical Request、ASL 与结果列在已支持的限定标量能力内保持一致。真实业务结果和原始回执只保存在 PRIVATE，不进入 Git。

本检查没有启动新的 Runtime、Trace、Cutover 或生产治理工作。
