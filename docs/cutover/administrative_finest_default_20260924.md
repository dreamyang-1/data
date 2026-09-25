# 同名行政层级默认采用细粒度

## 范围与根因

- 用户合同：同一地区同时匹配省、市等行政层级时，不再追问，默认采用已召回候选中的最细粒度；检查同类路径。
- PROVEN：Oagnet `_vector_semantic_ambiguities` 在模型调用前将相同 `attr_value` 的不同 `attr_code` 判为阻断歧义，生成截图中的“命中多个实体值”。上游 question_rewriter 已有同名行政层级细粒度选择，不能阻止此独立检查。
- PROVEN：Oagnet 的源值校准路径此前对泛指“地区”的同值候选优先省份；仅取消前置追问不能保证最终字段一致。
- 本次只修改 Oagnet，未修改上游提取、SQL Translator、配置或数据库。未部署或重启远程服务。

## 行为合同

- 对同一作用域、同一业务归属及相同规范值的省/市/区县候选，按区县 > 市 > 省选择已匹配层级。四个直辖市使用同一规则，不硬编码城市白名单，不靠向量得分或候选顺序选择层级。
- 未召回市级字段时不会凭空生成；不同规范值、不同归属地区（如医院与经销商）、已知不同父级编码、不同业务地址用途不折叠。同一最细层级仍有多个不同字段时，不利用本规则随意消歧。
- 用户显式层级或已经确认的字段优先，调用方权威 ASL 合同仍按原合同处理。
- 前置歧义检查、模型提示、源值校准和最终 ASL 字段统一规则。只重绑相同规范值的等值/IN 地区筛选；IN 要求全部值都支持相同目标字段。分组维度、指标、数值比较不变。
- 只有对应可执行筛选已存在，且模型追问的候选全部属于该同名行政层级集合时，才清理这条重复追问；不删除其他业务、时间或分组疑问。
- Schema、Semantic Scope、授权校验和六个主节点顺序不变。模型假数据集成测试保留真实 ASL 校验器。

## 变更清单

1. `Oagnet/agent.py`：行政层级选择函数及前置、源值校准、提示词、最终绑定入口。
2. `Oagnet/tests/test_administrative_defaults.py`：32 个新增用例，包括上海/北京/天津/重庆、三级候选、排序反转、显式 Pending 选择、负例、IN 及完整 main 路径。
3. `Oagnet/tests/test_relational_semantic_scope.py`：一个 STALE_TEST，泛指地区且省市同值时，旧省份预期按本次用户合同改为城市；其他约束保留。
4. 本报告。

## 测试与差异

| 检查 | 修改前 | 修改后 |
| --- | --- | --- |
| 全量，既有 MySQL/Milvus mock 运行器，限定 tests 目录 | 890 passed | 922 passed |
| 全量，仓库严格离线运行器 | 878 passed / 12 failed | 910 passed / 相同 12 failed |
| 严格离线专项：administrative_defaults + vector_semantic_ambiguity + relational_semantic_scope | — | 69 passed |
| 上游与 Critical Slice | — | 206 passed |

- 严格离线 JSON 用例逐项对比：old-pass → new-fail = 0，old-fail → new-pass = 0；新增 32 项全部通过；两次均无 collection error。
- 严格运行器的 12 项旧失败来自未补齐的存储/数据库测试替身：2 项实体值 API 调用到拒绝访问的 store；其余源值/实体属性测试缺少数据库配置或替身。修改前后失败测试 ID 完全相同，本次不修改这些无关测试或服务功能。
- 初次 baseline 命令未限定 tests，误收集备份文件产生 4 个收集错误；随后统一限定 tests 重新跑基线，不将误收集计入产品回归。
- Critical Slice：`test_question_rewriter.py`、`test_pending_execution_transition.py`、`test_phase0b_critical.py`、`test_api.py`、`test_workflow.py`、`test_bridge_event_lifecycle_contract.py`、`test_bridge_event_lifecycle_wiring.py`、`test_asl_surface_handoff.py`。覆盖上游既有省市处理、Pending 恢复与可见节点合同。

## Review 与状态

- Review：仅使用本轮授权候选，不扩大召回作用域；不改既有 API、SSE 和查询形状；显式选择覆盖默认值；没有生产数据写入或凭据提交。
- Current Stage：本项 Oagnet 修复及离线验证完成，进入功能分支评审；不代表生产验证完成。
- 本项 P0/P1：无新增离线阻塞。已知阻塞：严格全量的 12 项旧测试替身问题；线上实际目录、模型及完整业务查询尚未验证。
- Catalog/Evaluation/Shadow Gap：本次未推进目录发布、模型评测或 Shadow；V1 Replacement Readiness 不变，不能据此宣布替代就绪。
- Next shortest blocking path：用户要求部署后，仅发布本项文件，重启 Oagnet，并复测截图问题及显式省级选择；不自动合并或切换 V2。
