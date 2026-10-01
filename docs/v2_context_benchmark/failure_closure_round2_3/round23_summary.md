# Real-Business Failure Closure Round 2.3

**ROUND_2_3_CONCRETE_ENTITY_CLOSURE_PARTIAL_SAFE**

RB50-37 的唯一 First Divergence 已定位为 `CURRENT_TURN_ENTITY_INSTANCE_LOST`。最小生产修改为新任务补上一条通用实体实例保存合同：只有当前显式 Entity mention、Catalog 唯一主名称字段和当前 Scope 下唯一 exact Source Value receipt 同时成立时，才使用已有 Filter/Binding/Proof 结构保存实例；Generic Entity Type、Group By、历史值、未证明值、歧义值和不匹配 receipt 均不会被提升。

专项 12 项和相关回归 219 项全部通过。正例覆盖产品、医院、省份以及 Subject+Instance；反例覆盖 Generic Type、缺失/歧义值、Scope、receipt、错误 slot、非当前 evidence 和 NEW_TASK 历史隔离。completed question 与 Canonical Request 消费同一个已证明实例。

Round 2.2 RB50-37 离线 replay 使用原两份模型响应，新增模型调用为0。旧冻结 Source Value artifact 不含目标商品名的 observation，因此结果为 `SAFE_BLOCKED_MISSING_FROZEN_INSTANCE_PROOF`。这关闭了已证明的 silent broadening 风险，但没有达到本轮要求的 `TASK_PUBLISHED + SPECIFIC_ENTITY_PRESERVED + SEMANTICALLY_CORRECT`。

`ROUND23_PRE_TARGETED_GATE=FAIL`，所以没有进行真实定向模型调用，也没有运行 RB50-10。Benchmark 仍保持 V1 15/37、V2 5/37、First Task Publication 2/30；未重算分数。

本轮没有修改 Provider、Dynamic Schema、Prompt、Catalog、Oagnet、SQL Translator、Redis、Session、8088 或 V1。模型调用、SQL、生产写入、Core50 和 V1 运行均为0。

下一最短阻塞是为目标商品的正式 `product_name` 字段取得当前 81/[205] Scope 下可冻结、可复核的 exact Source Value observation，再使用同一 replay 验证 Task、completed question 和 Canonical。该证据取得不属于本轮已授权的“禁止SQL/不扩大范围”边界，本轮到此停止。
