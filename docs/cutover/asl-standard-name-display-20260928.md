# ASL 筛选标准名称展示

## 原因和决策

PROVEN：Oagnet/structured_binding.py 先将商品名绑定为目录标准值；query_binding_review.review_bindings 随后按已选目录关系，通过 resolve_keys 将名称谓词转换为业务表外键。原始标准名称谓词与执行编码谓词已经保存在 asl_repair 的 RESOLVE_FILTER_BUSINESS_OWNER 中（previous_filter / resolved_filter）。页面原来直接渲染执行 ASL，因此显示一组编码，而不是匹配后的商品标准名称。

本次用户要求恢复原标准名称格式。只更改展示视图，不撤销执行端的归属绑定，不把中文值错误填入 product_code 字段，也不改变同名商品对应的编码集合或业务范围。

## 最小清单

- app/presentation/intent_recognition.py：渲染时复制 ASL，只对本次关系/字典绑定记录与当前谓词完全匹配的项，使用 previous_filter 恢复标准名称字段、运算符和值。无映射、过期映射、冲突映射或不可用元数据时保持实际谓词；不查询新目录、不猜名称、不添加内容审核。
- app/adapters/http.py：普通查询及 query_surface 的 ASL 展示入口均传入已有 asl_repair；普通查询的计划缓存已保存并读取该记录。ASL 缓存、SQL 翻译参数、执行参数、结果内 ASL 均继续使用原始编码版本。
- 页面显式说明“ASL 标准名称展示视图”，列出名称字段到实际执行字段的映射，避免将展示视图冒充实际 SQL 条件。SQL 调用记录仍如实显示执行编码。
- tests/test_asl_standard_name_display.py：14 项覆盖名称/多编码、集合/排除、缺失/过期/冲突/异常元数据、数字/型号/显式编码不变，以及两个入口展示名称但传给 SQL 的仍是原编码、ASL 在 SQL 阶段之前出现。

## 验证和范围

原有显示/HTTP/surface 专项 168 passed；加入本次用例后，显示、HTTP/surface、API、解析节点、关联范围、多任务和 Pending 专项 321 passed。
首次新集成用例的模拟响应缺少作用域/Intent-ASL 合同回执，补全模拟协议后 14 项全部通过；未为测试放松生产校验。
Review：展示层只读复制，不使用原问题替代标准名称，不更改编号与名称之间的执行关系；未改 Oagnet/SQL Translator 或语义库。
此前洞察提示词改动仍是独立提交；本次不会借展示修复顺带部署它。
V2 Readiness、Catalog/Evaluation/Shadow 状态不变。

## 全量结果与发布状态

- Critical 多轮/作用域/单域/Pending：229 passed。
- 本次全量：4366 passed / 11 failed，340.44 秒，收集错误 0。上轮记录为 4351 passed / 12 failed：新增 14 项通过；上轮时序用例本轮通过；旧通过变失败 0。
- 剩余 11 项仍为 test_mcp_analysis_runner.py 的既有配置属性/编排方法接口不一致，未修改、未跳过。
- 功能提交 e890656 已推送授权功能分支。按明确清单同步，4 个文件 SHA-256 一致，未纳入环境文件、凭据或生产记录。
- 49 只读预检查：http.py 为已知基线；intent_recognition.py 有同事的三类对话状态标签、完整补全问题展示改动，与本次 ASL 渲染变更不重叠。远程文件哈希 d29f69155d3b8dfded6fa539abb4faf8f5855811c4ce0a01cb2f03d1d7542036；后续发布需仅应用本次补丁并保留这些独立改动，不得整文件覆盖。
- 首次验收按“测试失败停止发布”合同暂停发布，仅完成远程读取和差异审查；后续用户明确回复“发布”，批准这批已知旧失败的本次发布例外，见下节。

## 用户批准后的发布

2026-09-28 用户明确回复“发布”。例外仅限这次 ASL 名称展示，不视为以后忽略测试失败的长期授权，也不顺带发布上一项洞察提示词。

- 发布包 recent-e6606d3-20260928-142758，功能代码 e890656；仅部署 intent_recognition.py 和 http.py。
- 对已审查远程哈希应用唯一非重叠补丁，保留同事的对话标签和完整补全问题展示逻辑。更新后两文件哈希分别为 95361bf7aae3b6855cea80239ddfd1178e283ada034fb12f3251a9bd9879a658、a591898c578aacd57c60ffe780fc058ec1a571c595f4d2c9b14873e9ffcfcd5b。
- 备份和发布清单位于 /root/.codex-deploy-backups/recent-e6606d3-20260928-142758；支持按清单回滚，未覆盖环境配置。
- 远程针对真实部署代码运行 307 项全部通过。选择 ASL 展示、HTTP/surface、解析节点、API、多任务和 Pending 相关测试；对同事已改的对话标签不运行本地旧文案断言，原有 ASL JSON/dimensions 展示断言仍运行。
- 仅 DataAnalysis 服务于 14:28:17 CST 重启，PID 4002090，HTTP 200 READY；向量健康检查正常、配置哈希不变。Oagnet 和 SQL 服务未重启。
- 发布后通过平台流式接口、新会话回放用户原来的经销商名单问题：61.6 秒 COMPLETED，七个主要节点顺序正确。解析节点 JSON 为 product.product_name = 空心纤维血液透析器，并显示标准名称视图说明；实际 SQL 继续使用原先六个商品编码。
- SQL 原始返回 92 行，结果整理后 22 条，与之前基线数量一致；无追问、无额外指标或默认时间。名称展示和实际编码执行均通过真实链路验收，不仅是离线渲染测试。
