# Oagent Contract-Resolved Ambiguity Closure

**OAGENT_CONTRACT_RESOLVED_AMBIGUITY_CLOSURE_COMPLETE**

## 问题与第一分歧

真实平台双轮链路中，V2 已正确完成上下文处理：首轮建立订单笔数、江苏省和 2025 年的 Task；追问“换今年”在同一 Task 上只把时间替换为 2026，并将“查询2026年江苏省订单笔数。”交给原 V1 执行链。失败发生在 Oagent 返回 ASL 后、SQL Translator 之前。

定向诊断固定同一份 Canonical 请求并在 Oagent 返回后强制停止，未调用 SQL 或数据库。4 次受控 Oagent 调用中有 1 次复现：模型同时返回一条“省份名称 / province_name 未找到属性映射”的 filter ambiguity；Oagent 的合同修复随后又依据当前发布目录的唯一映射，把完整谓词修复为 `dim_province.province_name = 江苏省`。最终 ASL 已包含正确指标、过滤和 2026 时间范围，但旧 ambiguity 仍存在，DataAnalysis 因 `ASL_AMBIGUOUS` 正确地 fail closed。

`FIRST_DIVERGENCE = OAGENT_CONTRACT_REPAIR_LEAVES_STALE_SAME_FIELD_AMBIGUITY`

旧实现只在 ambiguity 正文包含过滤值（例如“江苏省”）时删除它。真实波动响应只提字段，不提值，因此没有被识别为已经由唯一目录映射解决的旧警告。

## 解决方案

修复位于根因所属的 Oagent Intent-ASL 合同修复层，不在 DataAnalysis Bridge、V2 Context、V1、SQL Translator 或数据库增加补偿。

当且仅当以下条件同时满足时，删除同一过滤字段的旧 ambiguity：

1. 调用方的 Canonical/Intent 合同明确要求该过滤谓词；
2. 当前授权且已发布的 Catalog 把语义字段唯一解析到一个物理字段；
3. 最终 ASL 已包含该完整谓词，或合同修复刚刚把该谓词写入 ASL；
4. ambiguity 正文明确引用语义字段、完整物理字段，或有区分度的物理字段尾名。

无唯一映射、无完整谓词、不同字段的 ambiguity，以及只碰巧出现 `name` 等通用短尾名的 ambiguity 均继续保留并阻止执行。实现不清空全部 ambiguity，不猜字段，不放宽 Scope，也不改变时间和指标合同。

修复回执在实际删除旧警告时增加 `cleared_stale_ambiguities` 计数，保留可审计性。

## 修改点

- `Oagnet/agent.py`
  - 新增精确清理已解决 filter ambiguity 的内部函数；
  - 在“已有完全匹配谓词”和“唯一映射后补写谓词”两条路径应用同一规则；
  - 替换原来依赖过滤值文本的宽松清理。
- `Oagnet/tests/test_intent_asl_contract.py`
  - 覆盖真实的字段型旧 ambiguity；
  - 覆盖已有正确谓词的路径；
  - 保证无关字段 ambiguity 仍保留；
  - 保证通用物理尾名不会误清理无关警告。

## 离线验证

| 验证 | 基线 | 候选 | 结论 |
|---|---:|---:|---|
| `test_intent_asl_contract.py` | 33 passed / 1 existing failed | 37 passed / 1 same existing failed | 4 个新增正反例通过 |
| 受影响合同/安全集合 | 未单独重跑基线 | 107 passed / 1 same existing failed | 无新增失败 |
| Oagent 全量 | 697 passed / 1 existing failed | 701 passed / 1 same existing failed | old-pass→new-fail=0 |

唯一既有失败为 `test_metricless_attribute_detail_uses_registered_subject_event_time`，基线和候选均因测试替身不接受既有 `business_domain_id` 关键字而失败；它与本次过滤 ambiguity 修复无关，本轮不修改。

模型定向诊断共 4 次，均在 Oagent 返回后停止；SQL、数据库调用和生产写入均为 0。原始模型正文、请求合同和 JUnit 只保存在 PRIVATE，不进入 Git。

## 运行验收

离线 Gate 通过后只需同步并重启 Oagent 8021。DataAnalysis 8088 与 SQL Translator 保持原进程。真实平台在新会话复验：

1. “查询去年江苏省订单笔数”创建 V2 Task v1 并返回真实结果；
2. “换今年”复用同一 Task，执行问题保持“查询2026年江苏省订单笔数。”；
3. Oagent 返回的最终 ASL 不再携带已由唯一省份字段映射解决的旧 ambiguity；
4. SQL Translator、数据库和页面结果均到达。

在真实双轮通过前，本文件只声明离线合同闭环，不宣称运行态问题已关闭。
