# 远程运行代码同步到本地（2026-09-29）

## 范围与结果

PROVEN：按用户要求，只从既有部署环境拉取 DataAnalysis_Agent、Oagnet、
SQL Translator 的源码，不向服务器发布、不重启服务、不调整配置或语义数据。
起始版本仓为干净的 `9bba91c`，开发目录涉及文件与版本仓内容一致。

DataAnalysis_Agent 导入12个运行文件，合并5个测试文件；Oagnet和SQL
Translator的运行源码已一致，无须覆盖。最新一次增量纳入计算结果内部列处理、
计算摘要及整体洞察输入。2026-09-29 10:53前后复核186个受版本管理的运行
Python文件：开发目录与服务器SHA-256一致，版本仓除已有换行符差异外内容一致。
同步期间服务器有两次编排文件更新，均已再拉取；最后复核无运行源码漂移。

保留本地较新的测试、AGENTS约定及部署文档。服务器旧测试通过Git历史对象
确认，不用它们回退本地已补充的用例。4个双方有新增的测试按历史版本三方
合并，另新增1个远程测试。test_api合并冲突保留七节点、隐藏规划调用字段的
本地断言，同时保留远程的多任务展示和防重复事件用例。

环境文件、凭据、日志、缓存、egg-info、备份和实际业务问题记录均未纳入提交。
最后只读健康检查：三个服务均active，健康接口均HTTP 200。服务现有启动时间
由其他操作者决定，本次没有执行远程写入或重启。

## 变更清单

- app/api.py
- app/services/orchestrator.py
- app/analysis/synthesis.py
- app/domain/models.py
- app/intent/structured.py
- app/planning/task_dag.py
- app/presentation/intent_recognition.py
- app/semantic_v2/canonical_execution_bridge.py
- app/semantic_v2/context_contract.py
- app/semantic_v2/context_v1_execution.py
- app/semantic_v2/pipeline.py
- app/semantic_v2/recognition.py
- tests/test_api.py
- tests/test_intent_recognition_display.py
- tests/test_surface_planner_bypass.py
- tests/test_task_dag.py
- tests/test_v2_relation_reason_display.py

运行文件均直接采用远程版本，没有在本次同步中重写业务逻辑。新增内容包括
多任务整体洞察与计算任务、合并回复和附件、上下文关系说明及业务无关问题直答。

## 验证与已知差异

- 已有基线：4404通过、11失败（旧MCP配置/方法名称）。
- 首轮导入后全量：4400通过、31失败，0收集错误；合并新增16个用例。
  old-pass → new-fail：20；old-fail → new-pass：0；原11失败保留。
- Critical及近期功能专项：276通过，覆盖权限范围、候选确认、阶段顺序、
  名单总结、导出预览和SQL执行默认口径说明。
- 最后两次远程编排增量纳入后，复跑Critical及全部合并测试、洞察生成测试：
  486通过、3失败。这3项属于上述20项中的展示文案断言差异；其余专项通过。
  全量测试针对首轮快照，未将其冒充最终快照的全量结果。
- Git diff --check通过；精确文件清单和覆盖前备份保存在本地任务暂存区。

新增20项不一致分类：

1. 10项：远程CurrentTurnSemanticParse新增`off_topic: false`，旧快照全文比较
   没有该字段。
2. 5项：远程将ADD/REPLACE/REMOVE/CLEAR/CORRECT映射为CURRENT_TOPIC_FOLLOWUP，
   旧测试要求CURRENT_TOPIC_MODIFICATION。这是内部关系映射变化，不仅是改中文文案。
3. 5项：多任务规划/补全问题、上下文提示及“新问题”等展示文字改变，旧断言未跟进。

这些是导入既有远程代码后暴露的兼容性差异，不能据此声称全量回归通过。
本次只同步并记录，不修改同事业务逻辑，也不删除测试来消除失败。
特别是内部关系合并的业务影响，应由对应模块后续审查。

Current Stage：远程源码同步；无V2切流、索引重建或语义库操作。
Readiness：源码一致性验证通过；全量回归仍有上述已知差异，不宣称生产验收完成。
