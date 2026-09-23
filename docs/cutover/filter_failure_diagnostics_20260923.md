# ASL 筛选失败信息保留具体字段和值

## 证据与根因

- PROVEN：当日 16:28:25 的 Oagnet 应用日志记录 `ASL_FILTER_INVALID`，原因为 `ASL filter field was not validated by vector semantic scope`，同时 `field=None, details=None`。
- PROVEN：`_validate_vector_grounded_asl` 的 filters 分支抛出普通 ValueError，丢失正在检查的谓词。API 只能从报错文本分类，无法提供字段和值。这是该次提示信息丢失的 first divergence。
- PROVEN：DataAnalysis 原提示在缺少上游诊断时将目标回退为“当前筛选条件”，最终形成“筛选条件当前筛选条件……”的笼统文案。
- UNKNOWN：历史日志没有保存被拒绝的 ASL 谓词，无法判定该次是地区、厂牌还是金额条件失败。前置结构化参考被省略的日志不等同于最终失败谓词；不能据此猜测。

## 最小改动

- Oagnet：保留原校验与首次失败即抛错行为，改用已有 ASLValidationError，携带实际失败的 field/operator/value 和校验阶段。只复制这三个合同属性，不返回额外模型元数据。沿用 API 既有的有界 details 机制，不增加请求参数或修改响应封装。
- DataAnalysis：读取该失败谓词，显示具体条件、失败原因、未执行查询的状态，以及数据部门应检查的目录映射/召回配置。明确这是首先失败的条件，不把其他请求条件标记为失败。
- 对旧响应缺少字段和值的情况，明确提示诊断信息不足；不让用户重复输入既有条件，不假定全部条件有错。
- 同时修正诊断展示将所有运算符写成等号、将 0/False 显示为空的问题；不影响实际筛选运算。
- 不修改结构化提取、提示词、Workflow、SQL Translator、授权边界、节点名称/顺序、成功查询路径或生产配置。不删除导致结果范围变化的筛选条件。

## 文件清单与 Review

- Oagnet：`agent.py`、`tests/test_filter_failure_diagnostics.py`。
- DataAnalysis：`app/services/dependency_error_messages.py`、`tests/test_filter_failure_diagnostics.py`、本报告。
- Review：核对生产异常文本与源码分支；测试覆盖三个候选筛选项分别失败、API 502 details、HTTP 诊断透传与编排消息、数值 0/False、比较符、IN/BETWEEN、成功路径、首次失败及不泄漏额外元数据。
- 测试字段是合成夹具，不作为历史请求失败字段或真实目录映射的证据。
- 按上述清单逐文件同步至版本仓并比较 SHA-256；不同步环境、凭据、日志或业务数据。

## 验证

- Oagnet 专项：29 passed；全量基线 812 passed，最终 821 passed（新增 9 项）。
- DataAnalysis 诊断专项：28 passed；Critical Suite：269 passed。
- DataAnalysis 全量基线 4245 passed / 11 failed；最终 4253 passed / 11 failed，336.64 秒（新增 8 项）。失败集合与基线完全相同，均为 `test_mcp_analysis_runner.py` 既有 MCP 配置/方法缺失；未触及该模块。无新增失败、无 collection errors、旧失败转通过 0。
- 旧测试断言未改。Oagnet 与 DataAnalysis 分别提交；延续现有功能分支及 Draft PR，不自动合并。
- Oagnet 提交 `e66f93c`；DataAnalysis 与本报告另行提交。现有 Draft PR #85 仍为 open/draft。

## 状态与边界

Current Stage：本次错误诊断修复与离线回归；尚未部署或重启服务。
Cutover Blocker P0/P1、Catalog/Evaluation/Shadow Gap：本次不重评全局门禁；历史失败谓词仍为 UNKNOWN。
V1 Replacement Readiness：不切换 V2，保留现有路由。
Next shortest blocking path：另行授权部署后，用新请求检查页面展示的真实失败谓词，再针对实际目录问题处理；不能把本次诊断修复宣称为订单查询已经成功。
