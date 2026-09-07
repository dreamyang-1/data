# 会话与任务状态机规格

结构化 `ConversationState → TopicState → TaskState → TaskVersion` 是执行真相，原始对话仅辅助 Turn Resolver。新状态机不接管现有 Redis key。

## 不变量

当前显式值优先；ADD 保留旧值；REPLACE 删除同槽旧值；CLEAR 形成 `EXPLICITLY_CLEARED` 屏障；新话题不继承筛选；返回历史话题恢复指定版本；语义 scope 变化使绑定失效；指标/维度/筛选/时间变化使 Dataset 失效；仅 Limit/Projection 白名单可保留 Dataset；并发写必须 CAS；Pending 不劫持新任务；REFRESH 不改语义；REVISE 新建版本；active/last executable/last executed/last dataset 四个指针分离。

事件完整规格见 `state_transition_table.csv`，可运行最小纯函数位于 `app/semantic_v2/state_machine.py`。
