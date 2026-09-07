# 30个真实失败样本归因摘要

## 重放能力说明

- **CURRENT_FACT**：输入来自第一阶段保存的 `docs/data_agent_failure_cases.jsonl`，共30条真实历史评测失败。
- **UNKNOWN**：这些产物没有保存完整历史、WorkingMemory、候选池、模型原始输出、ASL、SQL和数据快照，因此无法进行确定性全链路重放。
- **CURRENT_FACT**：本轮完成30/30条“历史证据局部重放/归类”，0条伪装为完整在线重放。
- **INFERENCE**：24条能基于断言和现有代码定位首个高概率错误阶段；6条保留 UNKNOWN 或跨层不确定性。

## 分布

| 首个阶段 | 数量 | 典型问题 |
|---|---:|---|
| SLOT_MERGE | 5 | “再加上”导致旧槽位或新增指标丢失 |
| STRUCTURED_INTENT | 3 | 明细/指标/查询形态混淆 |
| ASL | 11 | 时间粒度、关系投影、字段完整性未进入计划 |
| DATASET_TRANSFORM | 5 | 排序/限制追问被当成新查询 |
| UNKNOWN_ASL_OR_RESULT | 3 | 排名、关系投影和列完整性无法区分首错层 |
| UNKNOWN | 3 | 无数据、目录语义或执行结果证据不足 |

## 根因链

1. 单一 `intent` 标签同时承担服务路由、分析目标和查询形态，导致意图正确并不代表计划正确。
2. 追问合并缺少 typed slot reducer，ADD/REPLACE/REMOVE/KEEP 依赖启发式。
3. 时间粒度、关系目标、比较和 ResultContract 未成为跨仓库一致的一等字段。
4. 结果校验能检查基本结构，却不能普遍证明“用户要求的列、粒度、顺序和数量均满足”。
5. Trace 没有保存候选集、拒绝原因、各阶段输入输出和目录版本，造成失败后只能跨层猜测。

逐例证据和保守标签见 `failure_trace_replay.jsonl`、`failure_stage_distribution.csv`。
