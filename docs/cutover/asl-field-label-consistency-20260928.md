# ASL 页面字段标签与查询结构统一

## 根因及合同

PROVEN：`render_asl_extraction_json` 仅在 JSON 之外补充 `dimensions / display_fields`，JSON 本体仍显示 `dimensions`；HTTP adapter 的 SQL 调用说明把同一槽位固定称为“分组维度”。因此无指标明细查询在前后两处看起来仍是分组，实际查询未回退。

用户明确允许将两个名字一起显示，并要求下方查询结构保持一致。本次只修改显示合同，不拆分或改名真实 ASL 协议字段。

## 明确清单

- `app/presentation/intent_recognition.py`：在已深拷贝的名称展示视图中，把 JSON 键 `dimensions` 改为 `dimensions / display_fields`，保留顺序、内容及其他字段；补充说明这是页面标签，实际接口仍为 `dimensions`。
- `app/adapters/http.py`：查询结构使用同一英文标签；增加展示用途，存在字段且有指标为 dimensions，无指标为 display_fields，空字段为 null。只修改 progress 文案对象，不触及 translator/executor payload。
- `tests/test_intent_recognition_display.py`、`tests/test_asl_standard_name_display.py`：旧 JSON 展示键与执行 ASL 完全相同的断言因本次用户授权成为 STALE_TEST，更新为“仅展示键不同、原对象不变”；保留名称转换、编码执行、非法绑定回退及节点顺序断言。
- `tests/test_executed_metric_display.py`：增加 12 项组合，覆盖明细/指标、字段为空/非空及旧指标元数据，不改变原测试的实际执行对象断言。

无需修改接口信息或协调 SQL 服务升级：接口请求/响应的 ASL、DataQueryResult.asl、SQL 和 scope 均保留。原标准名称展示/编码执行功能不变。多任务及普通/surface 路径均使用这些公共展示点。

## 验证与 Review

- 展示、HTTP/surface、解析节点、API、多任务及 Pending：342 passed。
- Critical：229 passed。
- 完整回归：4383 passed / 11 failed，309.43 秒，无收集错误。此前基线 4371 passed / 11 failed；新增 12 项通过，无新增失败、无旧失败变通过。失败仍为 test_mcp_analysis_runner 的既有 Settings 超时字段及 orchestrator 方法接口不一致，未修改或跳过这些用例。
- 没有增加字段绑定校验、数值转换或语义规则，也不改变主节点顺序。按测试失败停止发布约定暂未部署重启；上一轮“所有修改都发布”的例外已用于当时待发布的两批洞察提示词，不自行扩大为今后忽略失败的许可。
- 部署必须保留 intent_recognition.py 已审查的同事三类对话标签与补全问题展示改动；不得整文件回退。部署脚本已准备相对本轮前基线的非重叠补丁。

这是 V1 页面表达修复；V2/Catalog/Evaluation/Shadow 状态不变。

## 后续发布

2026-09-28用户明确要求全部待发布内容上线，本项0cf0537已与c10acb2合并发布。
远程同事的标签、补全问题和任务规划差量均保留；相关服务器测试545项通过，
数据智能体17:19:12 CST完成重启并READY。两轮真实名单/限条数追问均COMPLETED，
解析校验、调度执行、结果校验与洞察节点顺序正常。
详见同目录query-result-warning-policy-20260928.md的最新发布记录。
