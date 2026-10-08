# Real-Business Failure Closure Round 1 — Regression Result

## 最终代码验证

| 验证集合 | 结果 | 说明 |
|---|---:|---|
| 最终受影响测试集合 | **330 passed / 0 failed** | Source Value、binding、probe、Filter generation/initialization、RawTurn、Structured Edit、Context Slice、原接口 bridge 等 13 个相关测试文件 |
| A–E / completed-question 专项复核 | **11 passed / 0 failed** | 属于上述能力的重复专项核对，不与 330 相加 |
| Git 仓库提交前子集 | **119 passed / 0 failed** | 在 `E:/yy` 对已同步功能提交执行 |
| collection errors | **0** | 上述运行均无收集错误 |
| old expectation 修改 | **0** | 未修改旧测试期待 |

本轮没有再次运行 3500+ 项全量测试。测试范围按五个生产文件的影响面收敛，已有 Context Critical / API bridge 回归包含在最终集合中。Oagnet 与 SQL Translator 没有修改，也没有把历史测试数字冒充本轮重跑结果。

## 离线回放

| Case | 结果 | 外部调用 |
|---|---|---:|
| G81-074 | 仍安全拒绝；竞争 CLEAR 未被自动吞掉 | 0 |
| G81-080 | 越过 request-not-applied，停在冻结 Source Value lookup 证据缺口 | 0 |
| S81-007 | 越过 request-not-applied，停在冻结 Source Value lookup 证据缺口 | 0 |
| Ranking 诊断 | 未提交实验越过 binding 后停在 `group_by`/IR 合同，实验撤回 | 0 |

离线回放全部为版本化诊断，不计 Gold PASS，不执行 SQL，不访问 Redis，不写生产状态。主要 PRIVATE 回执 SHA-256：

- offline replay receipt: `b25fce15de8c8712f1ee24e68d6cad59fcb2d4dd4a75b6d38118cbee86c2866e`
- binding replay receipt: `34feeaf1f707c18213650c44c1491f34c1bf9c1e5894d07fa6506b8264c5b927`

## 模型调用记录

正式候选重跑使用冻结 Scope 81/[205]、业务时间 `2026-09-09T09:00:00+08:00`、Catalog version `3f9589b95d376ebdf9683425dcf6c646f2394b278718b033527bd5e08b64fe21`：

- 30 chains / 102 turns
- 177 次模型请求，HTTP 200 = 177
- CurrentTurn 102，SemanticEdits 75，第三模型阶段 0
- input tokens 2,437,975；output tokens 81,139；total 2,519,114
- SQL calls 0；production writes 0

正式重跑后的诊断调用共 14 次，全部与 Gold 评分隔离：第一次 5 次因 PowerShell 管道编码把中文替换为 `?`，标记为无效语义证据；随后 UTF-8 role 诊断 6 次、binding 诊断 3 次。诊断 input tokens 181,705，output tokens 6,248，total 187,953。

本轮全部模型请求合计 191 次，input tokens 2,619,680，output tokens 87,387，total 2,707,067。无模型重试、SQL、生产 Redis 写入、Catalog 写入或 V1 重跑。

## 文件边界

生产修改只有：

- `app/semantic_v2/recognition.py`
- `app/semantic_v2/recognition_initialization.py`
- `app/semantic_v2/source_value_recognition.py`
- `app/semantic_v2/source_value_repairs.py`

新增测试只有 `tests/test_v2_source_value_failure_closure_round1.py`。PRIVATE raw/capture、`.env`、日志、缓存和业务结果不进入 Git。

**REGRESSION_GATE = PASS_FOR_AFFECTED_SCOPE**

**FULL_REGRESSION = NOT_RUN**
