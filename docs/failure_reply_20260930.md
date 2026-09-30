# 无完成结果时的最终回复

- PROVEN：此前 `render_root_report` 只在存在 NEEDS_CLARIFICATION 时提前返回；全部 FAILED / SAFE_FALLBACK 仍落入三段式模板。单任务失败出口直接返回原因，不存在这一汇总漏洞。
- 修复：所有没有 COMPLETED / PARTIAL_SUCCESS 结果的任务组合直接说明原因或缺少的信息；隐藏由上游失败造成的 SKIPPED 重复说明，保留独立失败/跳过原因；忽略此时无效的模型报告元数据。已有部分结果和查询成功但为空的行为保持不变。
- 发布清单：`app/presentation/root_report.py`，以及前一提交 `b867573` 中 `app/services/orchestrator.py` 的共享追问回复修复。远程按三方合并保留其他修改。未包含暂停中的 SQL 全量结果改造。
- 测试清单：`tests/test_root_goal_report.py`、`tests/test_task_dag.py`、`tests/test_intent_asl_contract_matrix.py`，加既有关键回归。
- STALE_TEST：上海经销商筛选旧断言“业务城市”已与用户明确要求的“经销商所在地”冲突；实际分类输出为“经销商城市”。只更新该用例的两个测试预期，未修改查询代码。
- 专项及关键测试：249 通过。新增 17 项覆盖不同失败状态、空原因、无效报告元数据、依赖跳过及 DAG 不调用洞察模型。
- 全量基线：4479 通过、1 项旧预期失败。隔离发布工作树的最终回归：4497 通过（334.43 秒），无收集错误；旧通过到失败 0，旧失败到通过 1，新增通过 17。暂停中的未提交改动未参与本次验证。
