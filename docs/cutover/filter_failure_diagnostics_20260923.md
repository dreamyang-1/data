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

## 后续授权部署与真实重放（2026-09-23）

用户明确要求部署重启，并测试原订单问题。发布 `filter-diagnostics-0c85c03-20260923-171327` 只更新两份运行文件，发布前远程文件与 `ea7a98d` 基线一致，逐文件备份、语法检查、写入前漂移检查、原子替换及发布后 SHA-256 核验均通过。

- Oagnet `agent.py`：`fe5a5d43c46d575389fa876ce72b33ebfbc5086d9548d1f7d2c29c6330296eb1`。
- DataAnalysis `app/services/dependency_error_messages.py`：`9f50112a9cbe8832f02b58ecde4c3c47f3a486e20d8351319aaa432ddde6732e`。
- 远程实际源码的隔离 Mock 测试：Oagnet 9 passed，DataAnalysis 8 passed。17:13:57 的三条筛选失败日志来自此 Mock 测试，不能作为真实业务失败证据。
- 两个服务于 17:15:10 CST 重启。Oagnet PID 2283494 → 3050419；DataAnalysis PID 2283529 → 3050444；均 active、NRestarts=0。
- 开发机访问 Oagnet 根路径 UP、向量健康 true；DataAnalysis READY、所有 readiness profiles 通过、无降级项。未修改或重启 SQL Translator，未改目录、数据库和权限。
- 备份与清单保存在远程受限部署备份目录，按发布标识可定位；回滚前须重新核对文件无后续更新。

### 本次重放的实际卡点

使用独立测试会话、同一完整问题及 81/[205] 范围调用流式入口，不复用或修改用户原会话。45.5 秒返回 SAFE_FALLBACK，依次出现意图识别、规划、ASL 生成，未进入数据执行。与旧请求不同，本次 ASL 校验 PASS，失败发生在 SQL 翻译阶段。

- PROVEN：17:16:03 的 Oagnet 修复记录显示，数值条件的字面量被纳入普通实体值召回；多个不相关表的 ID/rn 同值精确命中，最终执行 `RANDOM_TOP_TIE`，选中 `product_dept_relation.id`，并加入对应等值筛选。数字同值不证明这些字段具备相同业务含义，也不能据此认定数据重复或脏数据。
- PROVEN：SQL 翻译器收到的 ASL 包含上述关联表 ID 筛选，而非用户要求的金额筛选；同时 metrics 为 `sales_total_including_tax`，投影为产品名称与城市，订单明细意图被变成汇总形状。
- PROVEN：翻译器返回 `SEMANTIC_SCOPE_MISMATCH` / `Plan entity is unavailable in the current domain`。
- PROVEN：将日志中的该次 ASL 原样重送翻译接口，仍失败；仅删除误绑定 ID 条件，或仅将其替换为 `sales_order.amount_with_tax` 数值等值条件，均成功生成 SQL。三个对照均仅调用翻译接口，未执行放宽条件后的查询，未返回业务记录；替换金额字段的对照仍保留原聚合形状，不能算正确订单明细查询已完成。
- PROVEN（源码）：`_apply_surface_mention_normalization` 按字面值匹配后可重写已有过滤字段，扩大召回及同分选择没有保持数值条件的业务字段身份。这是本次观察到的错误 ID 条件来源，不是 SQL 校验凭空制造的错误。
- UNKNOWN：16:28 的历史请求没有保存失败谓词，仍不能将本次选中的具体 ID 字段断言为那次相同的失败字段。独立会话及模型输出也可能与原会话不同。

Current Stage：诊断修复已部署并完成真实重放；订单查询仍未跑通，业务修复尚未实施。
Next shortest blocking path：在 Oagnet 保持数值比较所属字段与明细查询形状，禁止把金额字面量跨业务角色随机绑定到 ID；保留 SQL 范围检查，不用删除金额筛选来宣称查询成功。具体修复需按后续授权实施。
Cutover Blocker P0/P1、Catalog/Evaluation/Shadow Gap 及 V1 Replacement Readiness 不重评，不改变现有 V1/V2 路由，不自动合并 PR。
