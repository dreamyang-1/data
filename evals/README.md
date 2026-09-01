# 意图分类评测记录

常见业务表达的 100 例回归集为 `intent_realistic_100.json`，测试结果和限制见
`INTENT_REALISTIC_100_REPORT.md`。

Qwen 200 条评测由上述数据集和 `intent_realistic_extra_100.json` 组成，当前执行记录见
`QWEN_200_EVALUATION.md`。

评测日期：2026-08-19。数据集为 `intent_smoke.json` 中 30 条纯合成问题，不包含真实业务数据。

| 模型 | 原始模型准确率 | Schema 合法率 | P50 | 最大延迟 | 结论 |
|---|---:|---:|---:|---:|---|
| `qwen3.6-plus` 非思考模式 | 96.67% | 100% | 21.1s | 40.0s | 语义准确但不适合每条请求必调；作为复杂意图分类器 |
| `qwen3.5-plus` 非思考模式 | 93.33% | 96.67% | 19.7s | 36.7s | 精度和稳定性均无优势，不采用 |
| `qwen3.6-flash` | 未评测 | 未评测 | - | - | 当前项目密钥返回 `Model.AccessDenied` |
| `qwen3.7-plus` | 未评测 | 未评测 | - | - | 当前项目密钥返回 `Model.AccessDenied` |
| `qwen3.7-flash` | 未评测 | 未评测 | - | - | 当前项目密钥返回 `Model.AccessDenied` |
| `qwen3.7-max` | 未评测 | 未评测 | - | - | 当前项目密钥返回 `Model.AccessDenied` |

`qwen3.6-plus` 的一处误判是把“本月销售额同比增长率是多少”分类为 `METRIC_QUERY`。系统已增加强规则优先门禁：同比、环比、增长率、明细、口径、血缘、预测等明确表达不再调用模型，也不允许模型覆盖。

优化后在线路由实测：

- “查询本月销售额”：`RULE / METRIC_QUERY / 0.95`，端到端约 0.8s（包含本机 HTTP 与 Mock 查询）。
- “最近销售情况不太对，帮我看看”：`STRUCTURED_MODEL / ANOMALY_ANALYSIS / 0.85`，端到端约 5.4s。

该 Smoke Set 只用于发现明显问题，不能作为发布准确率。Gate A 前必须扩展为每个主意图至少 50 条，并覆盖真实业务术语、否定、多意图、多轮纠正、跨域同名指标和高风险请求；报告 Macro F1、混淆矩阵、高风险召回率、Schema/回退率和分位延迟。

Qwen3.7 系列开通权限后，使用同一脚本临时覆盖模型名即可公平复测，不需要修改生产配置：

```powershell
$env:DATA_AGENT_INTENT_MODEL_NAME='qwen3.7-plus'
python -B evals\run_intent_eval.py
```
