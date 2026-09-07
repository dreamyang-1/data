# Phase 2.5 数据分析智能体架构收口报告

## 0. 执行说明
本轮只建立独立 Schema、纯函数、离线 Shadow、治理 Overlay 与测试；旧链仍为权威。未提交 Git（任务明确禁止）。
## 1. 当前基线
Phase 2 为 1253/25/2；本轮初始收集发现 5 个收集错误，详见 baseline。
## 2. 本轮修改范围
新增 `app/semantic_v2`、`tools/phase25`、`specs/semantic_v2`、`docs/phase25` 和测试；仅对当前源码真实引用的 legacy 类型做兼容补齐。
## 3. TypedLogicalPlan v0.2
由 Pydantic v2 唯一生成 Draft 2020-12 Schema，顶层 `PlanEnvelope` 严格禁止额外字段。
## 4. 多轴意图与语义角色
DialogueAct、ServiceRoute、AnalysisGoal[]、QueryShape、CatalogType 与 SemanticRole 分离。
## 5. Payload联合类型
18 类 payload 使用 `payload_type` discriminated union；Chat/Control/Dataset/Lineage 不承担全局查询必填。
## 6. Slot Reducer
确定性 KEEP/INHERIT/SET/ADD/REPLACE/REMOVE/CLEAR/RESET_TASK；显式 clear 屏障与 CAS。
## 7. 会话状态机
Conversation/Topic/Task/TaskVersion/Pending/Dataset 分层，18 个事件有状态转移表。
## 8. 澄清决策
只有 USER_AMBIGUITY 创建 Pending；系统失败不向用户索取业务参数。
## 9. 指标代数与目录治理
11 个指标 Overlay 已生成；无法证明的可加性等字段保持 UNKNOWN。
## 10. 关系基数与Join治理
24 条关系形成建议 Overlay；目录绑定不被当成粒度安全证明。
## 11. 时间语义
强类型 anchor/range/grain/boundary/timezone/calendar/as-of/watermark；不改用户显式区间。
## 12. N-best候选与消歧
保留跨角色候选、拒绝原因和计划约束；模型不得创造 ID。
## 13. ResultContract证明链
四层 Proof 全部 PASS 才允许 COMPLETED。
## 14. ASL 1.0适配和语义损失
LOSSLESS/LOSSY_COMPENSATED/LOSSY_UNSAFE/UNSUPPORTED 明确阻断规则。
## 15. Trace V2
22 阶段+ERROR 的安全 Schema 已生成，未替换当前事件系统。
## 16. 测试失败处理
收集错误降为 0；当前全量 1452 passed / 32 failed。未弱化断言。
## 17. 金标和评测体系
完整 V2 金标 0，部分金标 200；没有为达到 80 条目标而编造。35 项评测阈值待基线校准。
## 18. Shadow Runner
离线 Runner 可读取 JSON/JSONL、gold/failure fixture 和本地快照，输出 REAL/MOCK/PARTIAL/UNAVAILABLE；不执行外部副作用。
## 19. 尚未解决的问题
目录业务口径、生产候选质量、完整失败 Trace、多租户压测、事件策略与部分旧测试仍需 owner 决策。
## 20. 是否满足进入Phase 0正式开发的条件
**CONDITIONALLY_READY_FOR_PHASE_0**。Schema/Reducer/状态/澄清/Trace/Overlay/Shadow 均已具备；但完整 V2 金标不足、遗留失败未清零、业务目录关键字段 UNKNOWN。
## 21. 精确下一步
先完成 Owner 元数据确认、全阶段 Trace fixture、遗留失败逐项合同裁决和至少高置信完整金标子集，再启动 feature-flag Shadow 双写。
## 22. 文件变更清单
见 `change_manifest.json`；生产主链未接入 V2。
## 23. 测试结果
V2/工具新增 39/39 通过；DataAnalysis 全量 1452 passed / 32 failed / 0 collection errors；Oagnet 210 passed；SQL Translator 101 passed；未调用真实服务。
