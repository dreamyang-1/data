# Execution-backed Context Anchor Validation

**EXECUTION_BACKED_CONTEXT_ANCHOR_OFFLINE_GATE = PASS**

本轮只关闭 `V1_EXECUTION_FALLBACK_NEW_TASK` 成功后无法接续短时间追问的问题。既有 standalone barrier 保留；只有 V1 返回 `COMPLETED`、没有 error code 且携带真实 `QUERY_RESULT` evidence 时，Bridge 才保存 `V1_EXECUTION_ANCHOR / PARTIAL / EXECUTION_BACKED` anchor。

Anchor 保留原始问题、实际执行问题、Semantic Model、请求及解析后的业务域 Scope、可证明的时间上下文、CurrentTurn evidence digest 和 V1 执行 evidence 引用。复杂关系语义仍保存在 opaque base question 中；V1 Canonical/ASL 只作为执行证据，不提升为 V2 语义真值。

后续短追问只允许一个当前显式 `time_spec REPLACE`，并要求引用或 MODIFY evidence。`换今年` 使用既有时间规范化合同生成 `2026年`，只替换或插入时间，其余 opaque 问题逐字保留。其他槽位、多个修改、Topic Shift、Scope 不一致或 anchor 链不一致继续澄清或 fail closed。

## A–G 验证

- A/F：复杂 standalone NEW_TASK 原问题原样交给 V1；成功后建立当前 anchor，首问 execution question 不变。
- B：随后 `换今年` 不再返回 `NO_SAFE_V2_STATE`，生成 `查询2026年空心纤维血液透析器产品合作的经销商名单。` 并交给 V1。
- C：较新的 fallback NEW_TASK 如果执行失败，会使更老 anchor 失去当前资格，短追问不得继承旧任务。
- D：V1 失败或缺少 `QUERY_RESULT` evidence 时不建立 anchor。
- E：当前明确的时间值覆盖 anchor 中旧时间；测试从 2025 年替换为 2026 年。
- G：已有完整 V2 TaskState 的普通路径不写 execution-backed anchor，原行为保持。
- 生产 CurrentTurn 路径在 Context Proposal 未解析时保留同一次调用的 parse evidence，Bridge 不会为 anchor 补全追加模型调用。

## 离线结果

- 新增专项所在模块：19 passed。
- 受影响集合 Git 基线：242 passed。
- 修改后同集合：245 passed。
- old-pass → new-fail：0；collection errors：0。
- 模型调用、SQL、生产写入、Benchmark：0。

## 运行态状态

8088 重启及真实平台新会话两轮验收尚未执行。完成部署后只验证首问和 `换今年`；不会修改 Oagent、SQL Translator、Java/platform、MODEL_WIDE Scope 或 V2 Query Shape。
