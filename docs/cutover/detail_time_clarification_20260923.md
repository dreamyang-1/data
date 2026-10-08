# 明细金额与时间歧义一致性修复

## 已证实问题与边界

- PROVEN：线上待澄清状态中，明细问题的“单笔销售额”被平台指标别名改写为“单笔含税销售总额”；请求仍为 DETAIL_QUERY，但摘要将上游指标和实体假设展示为已理解事实。
- PROVEN：同期 Oagnet 运行日志表明，先前标量修复已保留金额 `> 1000`，恢复 `metrics=[]` 与订单明细投影。因此本次不是数值条件再次跨表绑定，也不是展示标签改动引起。
- PROVEN：返回的 time_anchor 歧义声称“最近一年”没有 type，使用 last_month 占位并要求确认；surface 路径跳过旧时间归一化，明细形状修正后未重新解决时间与遗留歧义。DataAnalysis 收到非空 ambiguity 即停止 SQL 调用并转为追问。
- 不保存原始业务记录、会话身份、凭据或生产日志。线上历史原始模型 AST 未被完整记录，不将歧义文案等同于已证实的原始 time_context；本次用模拟占位 AST 覆盖该失败形态。

## 最小修复清单

### Oagnet

- `Oagnet/agent.py`：仅在 surface 路径明确恢复明细后，处理现有解析器可以肯定识别的时间范围；按当前主体的已召回日期属性唯一绑定，不用模型随意选择的锚点或其他实体日期授权。不硬编码订单表名或日期列。
- 一个日期候选或用户明确指定的日期名称唯一命中时，生成 range/custom 日期范围；只清除已经解决且不涉及其他槽位的 time/time_anchor/time_range 歧义。其他业务歧义保留。
- 日期候选缺失或不唯一时，清除不可执行的占位时间，给出当前日期候选或缺失说明，绝不以去掉时间条件换取执行。
- 不把旧解析器强加给所有 surface 请求：“近半年”“上个月”等未由本次确定性解析覆盖的表达保留现有模型路径；聚合、趋势和已确认全部历史范围不被改写。
- ASL 的 range/custom 结束日是含当天，SQL 翻译器生成小于次日的条件；命名年月的解析结果相应调整到周期最后一天，包括闰年二月，防止多查一天。
- `Oagnet/tests/test_surface_detail_time.py`：单元与实际 main 入口模拟回归，覆盖占位时间、残留歧义、日期角色、范围边界、未支持表达、主体隔离和全部历史范围。

### DataAnalysis

- `app/services/orchestrator.py`：DETAIL_QUERY 且明确“单笔/逐笔/每笔/每条/逐条”时，不套用聚合指标词汇改写问题；字段绑定继续交给规划与 ASL，指标汇总/趋势的已有别名规则不变。
- Oagnet 语义追问的文本前缀改为当前完整问题，不再把旧请求的指标/实体假设声称为已验证结果。Pending 保存、候选选择、范围授权和阶段顺序保持原样。
- `tests/test_detail_clarification_consistency.py`：7 项，涵盖明细金额原义、聚合别名兼容、候选展示及澄清摘要。

## Review 与验证

- 无新增请求/响应字段；无修改 SQL Translator、向量库、权限、数据库或生产路由；最终 ASL 仍接受原有向量与字段校验。
- 基线：Oagnet 873 passed；DataAnalysis 4261 passed / 11 既有 MCP failures（上一版本全量记录）。
- Critical Slice：175 passed，含 API/SSE、Pending 恢复、事件生命周期、surface 交接和工作流；新增 DataAnalysis 专项与既有平台别名测试 16 passed。
- Oagnet 最终全量：890 passed，11.99 秒；新增 17 项，无 old-pass → new-fail，无 collection errors（显式 tests 目录）。对应功能提交 `ef4fadd`。
- DataAnalysis 最终全量：4268 passed / 11 failed，636.01 秒；新增 7 项通过，失败节点集合与上一版本逐项比对完全一致，仍为 `test_mcp_analysis_runner.py` 的既有失败；old-pass → new-fail 为 0，old-fail → new-pass 为 0，无 collection errors。
- 初次 Oagnet 全量命令未限定 tests，误收集本地 `_backups` 中旧模块，出现 4 个 collection errors；已改为显式 tests 目录，不删除备份、不修改旧断言，最终结果以正确目录运行记录为准。
- 文件按清单同步版本仓并比对 SHA-256。功能分支 `fix/remote-chain-time-binding-clean-20260921`，Draft PR #85 已核实 open/draft；不自动合并。
- 本轮仅离线测试，未部署、未重启、未发起生产订单查询。模拟成功不等于线上端到端业务验收已完成。

## 阶段

Current Stage：明细时间与澄清一致性修复完成，专项、Critical Slice、两服务全量及差异核验完成；按服务分别提交功能版本。
Cutover Blocker P0/P1、Catalog/Evaluation/Shadow Gap：本次不重新评估全局门禁；既有 MCP 失败独立保留，不借本任务扩展修改。
V1 Replacement Readiness：不变，保持现有路由，不自动切流。
Next shortest blocking path：后续授权部署后，以新请求核对时间范围、日期字段、明细金额条件及 SQL。

## 后续授权部署：2026-09-23

用户随后明确要求部署重启。本节更新上述未部署状态，不把离线测试等同于实际业务查询验收。

- 发布标识 `detail-time-6968a2a-20260923-204956`，功能版本 Oagnet `ef4fadd`、DataAnalysis `6968a2a`。
- 仅部署 `Oagnet/agent.py` 和 DataAnalysis `app/services/orchestrator.py`。两个旧文件均与 `6711e32` 基线一致（允许 CRLF/LF 差异）；替换前再次核对原始字节哈希，未发现他人修改冲突。
- 备份与哈希清单：`/root/.codex-deploy-backups/detail-time-6968a2a-20260923-204956`。完成 AST、上传哈希及替换后哈希核验；设置失败回滚，实际未触发。
- 远程 Oagnet 明细时间与标量专项：**69 passed，2.85 秒**。向量库、数据库和模型为模拟依赖，测试 logger 不写生产日志。
- 远程 DataAnalysis 澄清一致性与平台指标别名专项：**16 passed，1.00 秒**。使用 mock 适配器与内存会话；临时测试进程使用 V1 环境，不更改服务运行模式。
- Oagnet 重启时间 **20:50:18 CST**，PID `3148136` → `3824713`；DataAnalysis 重启时间 **20:50:22 CST**，PID `3476328` → `3825061`。两者均为 active，NRestarts=0。
- SQL Translator 保持 PID `2051085` 及原启动时间，未部署、未重启。
- 服务本机与开发机检查：Oagnet `UP`，向量库 healthy=true，平台 `READY`；readiness profiles 全部通过，无 degraded capabilities。路由仍为 `V2_CONTEXT_V1_EXECUTION`。

部署后运行文件 SHA-256，与版本仓一致：

| 文件 | SHA-256 |
| --- | --- |
| Oagnet/agent.py | 34b948e242226f9bd52998a5490bc251d30fbf143f73b1321e92c2f67c1fe839 |
| app/services/orchestrator.py | 775babf2332aa51521733613a4a5ec5ba14149bede1656b3402770c26ff3c4b3 |

Current Stage：两个目标服务已部署重启，远程专项与健康检查通过。
Cutover/Catalog/Evaluation/Shadow 全局门禁不重新评估，V1 Replacement Readiness 不变，不自动合并 Draft PR。
Next shortest blocking path：通过新请求验收真实模型、目录、SQL 与订单结果；本轮未执行真实业务查询，也未清理或改写用户的历史会话/Pending。
