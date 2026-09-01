# Qwen 200 条意图评测执行记录

执行日期：2026-08-20

## 数据集

- `intent_realistic_100.json`：原有 100 条常见业务表达。
- `intent_realistic_extra_100.json`：新增 100 条不同表达。
- 合计 200 条，覆盖 15 类主意图。

新增样本包含指标别名、口语问法、否定表达、未来时间、混合关键词、结果形态冲突、数据安全和元数据问题，不是把原问题机械替换数字。

## 本次 Qwen 执行状态

- 模型：`qwen3.6-plus`
- 调用方式：直接调用 DashScope OpenAI-compatible `/chat/completions`
- 请求数：200
- 成功到达模型并返回合法 Schema：0
- 连接错误：200 个 `ConnectError`
- 结论：本次运行环境无法建立公网连接，没有形成有效的模型准确率结果。

这不是“模型准确率为 0%”。分类模型没有收到这些问题，因此准确率应记为“未测得”。评测脚本已经修正：当合法模型响应为 0 时，`execution_valid=false`，`accuracy=null`，避免把基础设施故障误写成模型误判。

## 在正常网络环境执行

```powershell
cd E:\YouoAgent\DataAnalysis_Agent
C:\Users\lenovo\miniconda3\python.exe evals\run_intent_eval.py `
  --mode model `
  --dataset intent_realistic_100.json intent_realistic_extra_100.json
```

生产实际采用规则与 Qwen 结合的 Hybrid 路由，优化后应优先执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\run_qwen_200_eval.ps1 -Mode hybrid
```

如需单独衡量 Qwen 提示词本身，再执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\run_qwen_200_eval.ps1 -Mode model
```

有效报告至少应满足：

- `execution_valid=true`
- `schema_valid_rate` 接近 1
- `error_counts` 中没有大面积 `ConnectError`、`HTTPStatusError`
- 再查看 `accuracy`、`macro_f1`、`per_intent`、`confusions` 和 `failures`

## 数据集基线检查

规则分类器对合并后的 200 条命中 162 条（81%）。这个数字仅用于发现新增样本确实带来了新的表达和混淆边界，不代表 Qwen 成绩。
