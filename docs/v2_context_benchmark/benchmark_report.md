# V2 Context Engine Real Business Benchmark

**V2_CONTEXT_ENGINE_BENCHMARK_COMPLETE**

本轮只评测冻结的 Core 50，没有重新解析业务问题原文，没有修改生产代码、8088、Redis Schema、V1/V2 Prompt 或评测口径。V1 使用真实 `DataAnalysisOrchestrator.handle` 与固定模型，查询端为脱敏 Mock；V2 使用真实 `RawTurnPlanner`、同一 Scope 81/[205] 与冻结 Catalog/Source Value 证据。两端均未执行 SQL。

V1原生合同不产出V2 canonical binding ID，也不直接消费V2冻结Catalog Artifact；因此“同Catalog”在本轮只能保证相同 semantic model/domain scope和同一目录版本背景，不能声称两端逐ID parity。比较范围严格限定为上下文、任务解析、完整问题和条件操作。

## 结论

在 **37 条两端共同可评 Case** 上，V1 为 **15/37（40.5%）**，V2 为 **5/37（13.5%）**。因此当前证据不支持“V2已经优于V1”；V1在该冻结集合上明显领先。

- V2 胜：2
- V1 胜：12
- 双方都正确：3
- 双方都错误：20
- 不可比较：13

各自分母仅作覆盖说明：V1 **18/45（40.0%）**；V2 **5/37（13.5%）**，另有 13 条 V2 评测阻塞。不同分母不能直接作为胜率对比。

V2 的两个明确改进 Case 是：取消旧任务后按医院等级统计医院数量，以及按医院统计含税金额/订单笔数并显示医院等级。V2的严格 typed contract 能防止静默错查，但当前大量真实表达被安全拒绝；安全拒绝不计语义通过。

## 分类结果

| 上下文能力 | Case数 | V1 PASS/可评 | V2 PASS/可评 | V2阻塞 |
|---|---:|---:|---:|---:|
| 首问理解 | 8 | 5/8 | 1/5 | 3 |
| 时间追问 | 5 | 0/5 | 0/4 | 1 |
| 条件替换 | 8 | 1/7 | 0/6 | 2 |
| 条件清除 | 4 | 2/4 | 2/4 | 0 |
| 指标新增ADD | 0 | 0/0 | 0/0 | 0 |
| 指标删除REMOVE | 0 | 0/0 | 0/0 | 0 |
| 指代理解 | 19 | 6/15 | 0/13 | 6 |
| 历史任务恢复 | 5 | 4/5 | 0/4 | 1 |
| 新任务切换 | 9 | 4/9 | 3/8 | 1 |
| 用户表达不完整 | 30 | 8/25 | 0/22 | 8 |
| 歧义澄清 | 3 | 0/2 | 0/1 | 2 |

## 失败类型

| 类型 | Case数 |
|---|---:|
| CANONICAL_MAPPING_ERROR | 14 |
| CONTEXT_STATE_ERROR | 4 |
| EVALUATION_ERROR | 13 |
| MODEL_REASONING_ERROR | 5 |
| TASK_RESOLUTION_ERROR | 11 |

`EVALUATION_ERROR` 包括三类：缺少两端共用的已执行 Dataset、缺少 typed Pending fixture、冻结 Source Value 观察不能覆盖真实原句。它们没有被记为V2生产语义错误。其余 V2 失败主要集中在 Source Value/Filter 的 Canonical Mapping、前置任务无法发布后的上下文目标解析，以及模型生成的Slot Operation不一致。

## 调用、时间与安全边界

| Engine | 会话链 | 用户轮次 | 模型调用 | HTTP 200 | 输入Tokens | 输出Tokens | 总Tokens | 轮次耗时总和 | 最慢轮次 | SQL | 生产写入 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| V1 | 30 | 102 | 96 | 96 | 274799 | 23053 | 297852 | 447.923s | 6.797s | 0 | 0 |
| V2 | 30 | 102 | 178 | 178 | 2463194 | 81196 | 2544390 | 1343.860s | 29.281s | 0 | 0 |

“轮次耗时总和”是并发任务各自墙钟耗时之和，不是整批实际经过时间，也不是生产 SLA。V2一轮通常包含两个模型阶段，因此不能用调用数直接与V1的单阶段调用作延迟比较。

## 评测方法和限制

- 固定业务时间：2026-09-09，Asia/Shanghai。
- 两端 Scope：semantic model 81、business domain [205]。
- 模型：qwen3.7-max，Thinking=false、temperature=0、retry=0。
- 评分只采用原始问题、历史和冻结评价标准；运行状态本身不等于正确。
- Core 50 中指标ADD和REMOVE均为0条，这是历史数据覆盖缺口，不能据此给出能力结论。
- Case 39只裁决已声明的“清除具体产品限制”轴，没有裁决“全部产品”应为标量还是按产品分组。
- 结果指代 Case 33–35、42没有共用真实Dataset；Case 50没有共用typed Pending，均不评分。
- V2冻结 Source Value fixture不足导致的Case保持不可评，不推断为生产错误。
- 本轮不执行SQL，不评价业务数值、ASL或SQL正确性。

## 冻结输入与复现

- 评测 HEAD：`d9e9a37df76e86af14dea2a1c5df52c9621e5f57`
- Core 50 SHA-256：`77e6ce417f63b8e364d55ca4b7426475c9bbea39eb45e72d9d5e7c8ba91e60b0`
- 详细输入 Hash：保存在同目录 `benchmark_run_manifest.json`。
- 每条裁决及两端观察：`core_50_result.jsonl`。
- 首次V2诊断曾错误地在任一拒绝后中止整条会话；该回执保留在PRIVATE的 `v2-incomplete-stop-policy`。正式结果来自修正为“拒绝不发布状态、后续输入继续”的102轮完整重跑，未修改生产代码。

本结果显示当前应优先修复通用 Source Value/Filter消费合同和真实多轮任务发布链，再重新运行同一冻结 Core 50。现在不应扩大到500条，也不应基于这份结果宣称V2可替代V1。
