# Execution-backed Anchor Time Edit Validation

**EXECUTION_BACKED_ANCHOR_TIME_EDIT_OFFLINE_GATE = PASS**

本轮只修正 execution-backed partial anchor 对短时间追问的校验。Catalog、MODEL_WIDE Scope、V2 Query Shape、Prompt、V1、Oagent、SQL Translator、Redis schema 和 Session 架构均未修改；Benchmark 未运行。

## 真实第一分歧

隔离的 CurrentTurn-only 诊断复用了真实平台同一条“换今年”请求、Scope 81 / MODEL_WIDE→[205] 和已保存会话状态，新增模型调用仅 CurrentTurn 1 次；SemanticEdits、V1、Oagent、SQL、状态写入均为 0。

实际 Parse 为：mentions 0、operation markers 0、temporal expressions 0、所有 explicit slot 列表为空、dialogue act 为 `CHAT`，Context Proposal 为 `UNRESOLVED`。旧 validator 首先要求恰好一个时间 mention 和一个 `time_spec` marker，因此直接返回 `V2_EXECUTION_ANCHOR_EDIT_UNSUPPORTED`；同时旧实现把 `explicit_slot_mentions` 中存在但为空的 schema 键也误算作其他槽位。

## 最小修正

- Anchor 校验改为以非空 explicit slots 与 marker slots 的并集计算 effective semantic delta，不再以 raw mention/marker 对象数量决定是否安全。
- 只允许 effective slots 为 `{time_spec}`，且只接受 `SET/REPLACE`；NEW_TASK、topic shift、metric/dimension/entity/filter/relation/ranking/projection 等非时间修改继续拒绝。
- 当模型 Parse 为空时，只在整句可以唯一解析为既有受治理自然日历表达，且其余文本完全属于通用编辑/查询辅助语法时恢复时间。混合或非时间句不猜测、不透传。
- 对外错误码保持兼容；内部 reason 区分 missing/ambiguous/non-time/multi-slot/topic-shift/unsupported-operation/unresolved 等。普通日志只写结构化 role、slot、operation、relation、布尔值和归一化时间，不写用户业务文本；完整诊断只留 PRIVATE。
- Opaque base question 原样保留；时间更新只生成新的 completed question，不重建复杂关系 Query Shape。

## 验证

通过：`换今年`、`改成今年`、`看今年`、`换2026年`、`改成去年`、`看上个月`、`换成第一季度`。

继续拒绝：时间加指标、改地区、产品加时间、重查指标以及 topic shift。原有 fallback 成功建 anchor、fallback 失败不建 anchor、较新失败任务阻断旧 anchor、current explicit time 优先、完整 TaskState 路径保持不变等合同均通过。

- 专项模块：33 passed。
- 受影响集合 Git 基线：300 passed。
- 修改后同一集合：314 passed。
- old-pass → new-fail：0；collection errors：0；新增 14 项。
- 语法编译：PASS。
- 运行态：等待仅重启 8088 后的真实平台新会话两轮验收。
