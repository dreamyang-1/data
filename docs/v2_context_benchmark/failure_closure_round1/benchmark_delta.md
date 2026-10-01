# Real-Business Failure Closure Round 1 — Benchmark Delta

冻结 Core 50 SHA-256：`77e6ce417f63b8e364d55ca4b7426475c9bbea39eb45e72d9d5e7c8ba91e60b0`。冻结 V1 没有重跑；候选 V2 使用相同数据集、Scope、业务时钟、Catalog snapshot 和原评分入口。

## 正式结果

| 指标 | 冻结 Baseline | Round 1 | Delta |
|---|---:|---:|---:|
| Common evaluable | 37 | 37 | 0 |
| V1 correct | 15/37 (40.5%) | 15/37 (40.5%) | 0 |
| V2 correct | 5/37 (13.5%) | 5/37 (13.5%) | **0** |
| V2 remaining failures | 32 | 32 | 0 |
| V2 wins | 2 | 2 | 0 |
| V1 wins | 12 | 12 | 0 |
| Tie correct | 3 | 3 | 0 |
| Tie incorrect | 20 | 20 | 0 |
| Not comparable | 13 | 13 | 0 |
| first-task publication | 2/30 | 2/30 | 0 |
| plan outcomes | 13/102 | 13/102 | 0 |
| direct fixed Cases | — | 0 | 0 |
| cascade recovered Cases | — | 0 | 0 |

正式 scorer 输出 SHA-256 为 `36472e9ba1134bf71e5d8bf49a8f6037a0c2f8960ff916ac1d61441a136c456b`。冻结 evaluator 及其既有 case verdict mapping 未修改。对候选新产出的 PLAN 逐项复核后，RB50-13、14、21 仍因所需前置产品/医院任务未发布而不符合期待，不能改判为 PASS。

## P0 错误族变化

下表单位是 102 个实际轮次中的错误事件，不是独立 Case 数：

| P0 reason family | Baseline | Round 1 | Delta |
|---|---:|---:|---:|
| `V2_SOURCE_VALUE_REQUEST_NOT_APPLIED` | 6 | 5 | -1 |
| `V2_SOURCE_VALUE_CURRENT_MENTION_REQUIRED` | 16 | 16 | 0 |
| `V2_BINDING_OUTSIDE_EDIT_EVIDENCE` | 6 | 6 | 0 |
| `V2_PAYLOAD_WOULD_DROP_SEMANTICS` | 3 | 2 | -1 |
| **合计** | **31** | **29** | **-2** |

P0 事件下降没有转化为完整 Case PASS。部分 Case 只移动到下一安全拒绝边界，例如 frozen source observation、slot operation conflict、time evidence 或 recognition unresolved。没有把后续拒绝计为修复成功。

## 运行成本对照

| V2 运行指标 | Baseline | Round 1 |
|---|---:|---:|
| 模型请求 | 178 | 177 |
| input tokens | 2,463,194 | 2,437,975 |
| output tokens | 81,196 | 81,139 |
| total tokens | 2,544,390 | 2,519,114 |
| turn wall sum | 1,343.860s | 1,233.657s |
| max turn | 29.281s | 24.516s |

这是一次非确定性 Provider 小样本对照。虽然调用和 token 数略低，本轮不据此声明性能改善；业务正确率优先且没有提高。

## 门禁结论

用户要求的 `V2 > 5/37` 未达到，接近或超过 V1 的 `15/37` 也未达到。P0 错误族没有显著下降，Task publication 与 cascade recovery 均没有改善。

**BENCHMARK_IMPROVEMENT_GATE = FAIL**

**V2_CONTEXT_ENGINE_BENCHMARK_ROUND1 = PARTIAL / NOT_CLOSED**

**V1_PRODUCTION_ROUTING = UNCHANGED**
