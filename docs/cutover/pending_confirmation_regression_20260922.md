# 候选确认与合并后回归修复

## 范围和基线

Current Stage：按用户本轮要求修复候选确认并更新过时测试，不推进 V2 切流。
基线为 `736524d`，功能分支为 `fix/remote-chain-time-binding-clean-20260921`，沿用 Draft PR #85。
只修改 DataAnalysis 编排中的候选清理调用链、相关测试，并补回远程已有的提示词资源。
不改 Oagnet、SQL Translator、数据库、授权范围、正式模型或服务器进程。

## 根因与处理

- **PROVEN：候选确认误清理。** `_apply_semantic_clarification_choice` 原来按整个槽位类型删除歧义，确认一个指标会清掉另一个指标；`_suppress_confirmed_slot_ambiguities` 随后的去重也会重复误清理。改为按成员定位，仅清理对应项；无法直接定位的重复问法须有相同目录候选身份，不能仅凭显示名称。保留原来对同一问题简称/重复问法的去重。
- **PROVEN：闲聊正式接口已经兼容。** 合并后的 responder 已接受 `agent_prompt`，没有再次修改正式接口；补充 MockTransport 断言，验证提示词确实进入请求的 system message。其他旧 intent mock 同步接受该参数，避免参数错误进入失败回退。
- **STALE_TEST：类型化过滤条件。** 地区已经在 `filters` 中传递，不应再要求重复出现在无类型的 `semantic_entity_mentions`。新断言仍验证实际过滤值和完整 filters，没有删除条件保留检查。
- **STALE_TEST：候选确认的问题文本。** 所选含义写入补全后的问题，原始问题保持不变；保留完整问题中的指标、时间断言。
- **STALE_TEST：桥接事件。** 测试 fixture 补上正式入口已有的 open/close 生命周期回调，事件断言不变。
- **STALE_TEST：展示与上下文交接。** 当前意图展示只保留原问题/补全问题，参数和意图在任务规划展示；测试改为校验结构化 view 中的精确内容及不重复展示。空上下文延期提取 fixture 同步当前 schema，不再伪造上游负责提取 mentions 的旧响应。
- **STALE_TEST：测试隔离。** 替换假目录权威前清理该测试进程的目录缓存，避免相同模型/域复用另一个测试的数据；正式缓存行为不变。
- **PROVEN：漏同步运行资源。** `task_dag.py` 依赖的 `app/planning/structured_extraction_prompt.txt` 本地缺失，而远程实际存在。按原样恢复，不修改提示词，不删除两项内容断言。恢复文件 25,858 字节，远程/开发目录/版本仓 SHA-256 均为 `8e2ab65d59b996f5eccedadc20773dddecfdced978e7c63d8157184cd5b5bb71`。

## 验证

专项基线 51 failed / 213 passed；更新后对应专项 269 passed。
最终候选、闲聊、展示、上下文专项 201 passed。
Critical Suite 212 passed，覆盖 API/SSE 主节点顺序、候选、原任务恢复、事件生命周期和分析编排。
新增 7 个回归案例：指标/维度、候选 ID 缺失/不同/复用及后续重写误清理。

全量命令：`python -m pytest -q --tb=short --durations=5`，在版本仓运行离线测试。

| 结果 | 基线 | 本轮最终 |
| --- | ---: | ---: |
| Passed | 4145 | 4220 |
| Failed | 79 | 11 |
| Collection errors | 0 | 0 |

Old-fail → new-pass：68；old-pass → new-fail：0；新增测试：7，全部通过。
全量耗时 245.55 秒。剩余失败未 skip/xfail，完整保留。

## 已知边界

MCP 通用分析模块与当前入口存在接口不一致：`mcp_analysis_runner.py` 读取不存在的 `mcp_analysis_*` 配置；测试还调用不存在的 `_should_dispatch_mcp_analysis` / `_run_mcp_analysis`。当前正式入口接入的是 `mcp_file_analysis_*`。已纠正测试请求漏传必需 `semantic_model_id` 的问题，但不伪造其余配置/方法或静默跳过失败。需该模块负责人确认旧模块应补齐接线还是退役，本轮不替同事做此决策。

剩余 11 项均位于 `tests/test_mcp_analysis_runner.py`：

- 6 项在 runner 读取 `mcp_analysis_tool_timeout_seconds` 时失败：multi-turn tool loop、tool failure、turn limit、unknown tool call、skill loading、missing skill warning。
- 1 项在 `_should_dispatch_mcp_analysis` 失败：dispatch gate。
- 4 项在 `_run_mcp_analysis` 失败：response assembly、execution truncation、runner error fallback、nothing usable fallback。

Cutover Blocker P0/P1、Catalog/Evaluation/Shadow Gap：本轮不重新评估全局切流资格；这些离线结果不能替代生产证据。
V1 Replacement Readiness：不宣称生产就绪，不切流、不合并 PR、不部署服务。
Next shortest blocking path：明确通用 MCP 与文件 MCP 的维护边界，再修复对应模块/测试；本轮改动如需上线，另行执行部署及运行验证。

## 明确同步清单与审查

按以下清单由开发目录同步到版本仓，逐文件比较 SHA-256；不复制环境、日志、缓存、备份和凭据：

- `app/services/orchestrator.py`
- `app/planning/structured_extraction_prompt.txt`
- `tests/test_bridge_event_lifecycle_contract.py`
- `tests/test_candidate_integrity_contract.py`
- `tests/test_chat_responder.py`
- `tests/test_dimension_mutation_contract.py`
- `tests/test_intent_recognition_display.py`
- `tests/test_mcp_analysis_runner.py`
- `tests/test_metric_edit_grounding_contract.py`
- `tests/test_pending_execution_transition.py`
- `tests/test_product_clear_contract.py`
- `tests/test_semantic_choice_contract.py`
- `tests/test_structural_scope_contract.py`
- `tests/test_v2_context_v1_execution_bridge.py`
- `tests/test_v2_raw_turn_recognition.py`
- 本报告。

自审重点：未引入新的请求字段/SSE 格式；未改变节点顺序；未扩大授权或改写原始问题；已确认项不重复追问，其他待确认项不丢失；测试调整对应当前正式接口与结构，不以删断言制造通过。
