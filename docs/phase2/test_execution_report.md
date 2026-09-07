# 第二阶段测试执行报告

检查日期：2026-09-07；环境：本机集成开发环境，Python 3.12；所有命令设置 `PYTHONDONTWRITEBYTECODE=1` 并禁用 pytest 缓存。未运行会写数据库、重建索引或调用真实模型的测试。

## 结果

| 范围 | 结果 | 模式 |
|---|---:|---|
| DataAnalysis第一阶段核心249项 | 242 passed / 7 failed | mock/isolated，无真实外部调用 |
| DataAnalysis扩展语义与多轮743项 | 725 passed / 18 failed | mock/isolated，无真实外部调用 |
| Oagnet安全选定单测185项 | 185 passed | mock/isolated；排除daily/live/Milvus写入类 |
| SQL Translator单测101项 | 101 passed | isolated |
| DataAnalysis收集检查 | 2 collection errors | 缺失`BusinessRuleRef`、`ResultValidationReport` |

汇总（实际执行用例，不重复计算收集失败文件）：1253 passed，25 failed，0 skipped；另2个测试模块收集失败。

## 失败簇

- 核心7项：ContextBuilder/ExtensionExecution Schema漂移2项、memory事件1项、trace/evaluator事件Schema漂移4项。
- 扩展18项：澄清规范日期与指标、缺失槽位去重和grounding、PendingState转换、语义澄清恢复及中断语义。
- 两个收集错误表示测试仍引用已移除模型，不属于本轮审计材料引入。

## 命令证据

核心与扩展均使用 `python -m pytest -q --tb=no -p no:cacheprovider <明确文件列表>`；Oagnet排除了 `daily_*`、`live_*`、`milvus_*` 和实体集成写入测试；SQL Translator执行仓库内8个测试文件。终端原始摘要分别为 `7 failed, 242 passed`、`18 failed, 725 passed`、`185 passed`、`101 passed`。

## 边界

**UNKNOWN**：未执行真实模型、生产数据库写入、Milvus重建、全链路数据正确性和多租户并发压测，因此这些结果只能证明隔离单测现状，不能证明生产业务答案正确。

