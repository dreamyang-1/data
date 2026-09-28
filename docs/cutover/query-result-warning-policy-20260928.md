# 已返回数据不被分析校验吞掉

## 范围与根因

Current Stage：V1 最小链路修复；不涉及 V2 切流、语义库或索引变更。

PROVEN：用户提供的两轮执行记录中，第二轮“展示前10条”仍是明细查询，
ASL 无指标、LIMIT=10，SQL 已成功返回10行，名称清理后8行。
旧分类器把 LIMIT 当成 TOP_N，编排随后调用指标排名分析；缺少排名指标抛出
AnalysisError 后进入 SAFE_FALLBACK，导致已经查到的名单没有交付。

该问题与数据库是否存在经销商无关。“LIMIT 后清理重复名称可能不足10行”
是另一个行为，本次不伪造或补足记录，也不改动名单清理和SQL排序口径。

## 改动清单

- app/intent/classifier.py：明细的数量限制不再生成指标 TOP_N/BOTTOM_N 算子。
- app/services/orchestrator.py：结合已执行 ASL 判断是否实际含指标；纯明细不进入排名计算。
  分析条件不足、分析计算失败、缺少指标证明及数据质量提示不再吞掉已返回的表格和附件。
  截断数据只做预览解读；无附件的超大结果保留20条预览而不是终止。
- app/adapters/http.py：解析端的扩展分析确认、执行端的分析条件确认/数据形状检查，
  改为已有 execution_transforms 内的分析告警，继续正常查询/交付。
  带解析告警的计划不写入正常 ASL 缓存。
- tests/test_analysis_orchestration.py、tests/test_analysis_data_contracts.py：新增正反例，
  包含明细限条数、遗留 TOP_N、质量 FAIL、缺失指标证据、超大预览、缺失分析确认及阶段顺序。
- tests/test_export_preview_failure.py、tests/test_forecast_readiness.py、
  tests/test_external_search_routing.py：按新交付约定更新旧终止断言，并保留真实排名/公开信息补充测试。

没有新增请求字段或响应结构。接口、SSE 七节点顺序保持不变。
Scope、数据源身份、只读SQL、实际不可执行字段和查询条件正确性检查不在本次删除范围；
不把“任何非必要校验不拦截”实现成跨权限查询或静默更改筛选范围。
数学分析器仍拒绝无依据计算，但错误转为说明后继续交付原始查询结果。

## 验证与旧断言分类

- 专项：445 passed。
- Critical Suite：229 passed；另加解析节点顺序3项，共232 passed。
- 修改前全量基线：4383 passed / 11 failed，全部旧失败位于 test_mcp_analysis_runner.py。
- 首轮全量：4387 passed / 16 failed（当轮9个新增参数化测试；另一个新增用例随后加入）。
  额外5项中，超大结果、预测不足、画像分析失败的终止断言属于 STALE_TEST；
  两个画像排名测试的模拟请求没有任何指标，却由桩直接返回虚构排名，补齐明确的业务规模指标后继续保护原排名行为。
  同时把是否分析的判断锚定已执行ASL，保留旧路径中 DETAIL 分类但确实选有指标的画像排名。
- 新增用例共10项。最终全量：4393 passed / 11 failed（272.33秒）。
  11项失败与基线相同：6项引用已不存在的 mcp_analysis_tool_timeout_seconds，
  5项引用已不存在的 _should_dispatch_mcp_analysis / _run_mcp_analysis。
  本次没有改动这些模块；无新增回归，无收集错误。
- 无收集错误；第一次 Critical 命令缺少 SQL translator 导入路径导致17个 setup errors，
  修正运行命令后232项全部通过；没有为此修改产品代码。

## 发布与风险

发布前只读检查发现服务器编排文件存在同事增加的追问判定说明、任务依赖展示、
轮次中文标签等变化，必须按本次差量合入，禁止以本地文件整体覆盖。
服务器SQL输入展示字段标签也与本地存在已知差异，不能顺带覆盖。
当前尚未部署；全量测试基线的11项旧MCP失败须取得本次发布例外，或修复后再发布。
已向用户询问本次例外，未收到确认前不操作服务。
未更改服务器配置、权限或运行服务。

Cutover Blocker / Catalog / Evaluation / Shadow：本次未重新评估，保持既有记录，
不能由这些V1测试推断V2已具备替换条件。Next shortest path：确认全量测试差量，
提交本次最小补丁；得到发布例外后合入远程差异并验证真实两轮追问。
