# Real-Business Failure Closure Round 2.4

**ROUND_2_4_ENTITY_PIPELINE_FAILURE**

真实 Source Value 取证和离线实体闭环均已完成，但新的 RB50-37 模型响应未通过 SemanticEdits 动态 Schema，因此 Round 2.4 不能宣布真实实体闭环成功。

| Gate | 状态 |
|---|---|
| REAL_EXACT_SOURCE_VALUE_PROOF | `FOUND_UNIQUE` |
| SOURCE_VALUE_LOOKUP_PATH | `PRODUCTION_COMPATIBLE_READ_ONLY_PATH` |
| ROUND24_OFFLINE_ENTITY_CLOSURE | `PASS` |
| RELATED_REGRESSION | `219 passed / 0 failed / 0 collection errors` |
| RB50-37 REAL TARGETED | `FAIL_SAFE_REJECT` |
| FIRST FAILURE | `SEMANTIC_EDITS_DYNAMIC_SCHEMA_VALIDATION` |
| TASK PUBLISHED | `NO` |
| PRODUCTION CODE CHANGES | `0` |
| BENCHMARK SCORE CHANGES | `0` |

Source Value 路径在 81/[205] 下对 `product.product_name` 返回唯一 exact normalized value，并生成完整、可离线回放的 proof。使用 Round 2.2 已录制的两阶段响应进行断网回放后，具体商品过滤、Task、Payload、Canonical Request 和 completed question 全部一致，没有扩大成全部产品。

只有在上述 Gate 通过后才执行新 Live。模型调用严格为 CurrentTurn 1 次、SemanticEdits 1 次、retry=0。CurrentTurn 保留了具体商品 surface；SemanticEdits 却生成本次动态 Schema 明确禁止的 Source Value request/field handle。客户端在入口安全拒绝，Source Value lookup 未到达，Task 未发布，也没有发布错误的全产品查询。

这说明 Round 2.3 的实体保留实现不再受缺少真实 proof 阻塞，但新的模型输出与其动态 Schema 仍未对齐。该问题不属于本轮允许修复的 Source Value lookup/normalization 缺陷。本轮按停止条件保留为 `ENTITY_PIPELINE_FAILURE`，不修改生产代码、不追加模型调用，也不启动下一 Root。

本轮未运行 RB50-10、Core50、V1、8088、业务 SQL、完整 Benchmark、Oagnet 修改或 SQL Translator 修改。V1 生产路由和当前平台进程保持不变。

详细证据见 [Source Value 取证](SOURCE_VALUE_PROOF_ACQUISITION.md)、[离线回放](RB50_37_OFFLINE_REPLAY.md) 和 [真实定向结果](RB50_37_TARGETED_RESULT.md)。
