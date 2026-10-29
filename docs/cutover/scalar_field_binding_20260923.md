# 数值/字母条件与订单明细形状修复

## 证据、边界与根因

- PROVEN：前一轮真实重放将金额字面量跨表选成关联记录 ID，且订单明细变为销售额汇总；翻译器因不可用实体拒绝。翻译对照仅替换误绑定条件即可通过，不代表完整订单查询已经正确。证据见 `filter_failure_diagnostics_20260923.md`。
- PROVEN：DataAnalysis 已向 Oagnet 发送 `structured_extraction`，但 Oagnet QueryRequest 未声明该字段，Pydantic 默认忽略额外键，生成入口没有收到完整规划提取。旧 `structured_reference` 可能仍携带较粗的分类器猜测。这是输入链上的 first divergence。
- PROVEN：surface 归一化按字面值查询候选，可扩大到不相关 ID 并随机选择同分值，再改写 ASL 的 field/value。不同字段恰好具有相同数字或字母并不说明业务身份相同，也不等于数据部门的重复数据问题。
- 本次只修改 Oagnet 的既有输入消费、生成及归一化链。DataAnalysis 结构化提取、workflow、SQL Translator、Semantic Scope 和生产环境未修改。此前授权部署的版本仍在线，本轮功能修复尚未部署。

## 实现

1. API 接收并转交调用方已经发送的完整提取 JSON。其内容与补全问题一起进入提示；只提取已知业务槽位作为参考，不读取其中的权限或范围作为授权。没有新提取时保持原引用兼容路径。
2. 根据当前目录字段元数据恢复金额/数量/状态/编号等比较，保留字段、运算符和值的绑定，不要求阈值必须存在于实体值目录。范围、IN/NOT IN、零、负数、小数和同字段多个边界保持原义。
3. 明确的数值字段支持规范千分位、科学计数法和正负号；小数不经浮点数舍入。ID/编码字段保留字符串及前导零。含单位的未规范值、非有限数等不猜测换算，返回具体字段和值的诊断。
4. 对数字/字母混合名称的候选扩展排除无关 ID/rn 和数值业务字段；型号/编码仍可在合适的名称、规格等字段间匹配。完整 token 可匹配较长标准名称，不接受 token 内子串替换，例如 M60 → M600、AB → ABC、AT75242 → 7、0012 → 12。
5. 数值比较不参与普通实体名称召回；汇总阈值保留 HAVING，不误变成行级 WHERE。时间和条数限制不是值过滤。相同值用于多个字段时分别保留；已有名称条件不能因与数值条件同值被清掉。
6. 当完整问题明确要求明细、上游为无指标明细参考且有目录确认的返回列时，恢复 metrics=[] 和返回列，不因“销售额”一词改为 SUM。汇总、趋势等明确请求不受此明细修复影响。最终字段仍经过原向量授权与 ASL 校验。

## 文件与 Review

- `Oagnet/api.py`：接收已有传入字段并转交。
- `Oagnet/surface_literals.py`：纯函数处理规划参考、字面量与数字格式，无目录或权限副作用。
- `Oagnet/agent.py`：目录字段绑定、型号候选边界、明细修复与原入口接线。
- `Oagnet/tests/test_surface_literal_constraints.py`：新增 52 项。
- 本报告。明确清单逐文件同步至版本仓，比对 SHA-256；不复制环境、凭据、日志或原始业务记录。
- Code Review：无硬编码业务表/金额字段映射；不扩大授权；不修改其他人的提取模块；无有效单笔金额条件被静默省略；字段缺失仍给出可定位诊断。原始输出与新修复均受既有最终向量校验约束。
- 初次离线回归发现“品牌+完整型号”到标准编码的既有行为受影响，已收窄为完整 token 比较而非一律禁止包含匹配。原有测试断言未改；品牌/规格/同表纠错与名单等旧路径全部保持通过。

## 验证与差异

- Oagnet 全量基线 821 passed；最终 873 passed（新增 52 项），无 collection errors，无旧通过转失败。
- Critical Suite 269 passed，包含主节点顺序、会话候选恢复、surface 交接、可选展示字段和结果清理。
- DataAnalysis 全量：4253 passed / 11 failed，273.44 秒。与基线通过数及失败集合一致；失败仍全部为既有 `test_mcp_analysis_runner.py`，本轮无新增失败、无收集错误，旧失败转通过为 0。
- 不在本轮执行生产写入、目录更新或服务重启。离线模拟覆盖 API 传参、模型提示与错误 ASL 草稿经实际归一化/最终校验的主入口；不能替代真实业务验收。

## 状态

Current Stage：本次 Oagnet 数值与字母条件修复、离线回归及功能分支收尾。
Cutover Blocker P0/P1、Catalog/Evaluation/Shadow Gap：本次不重评全局门禁，历史失败谓词仍不能回填为已知。
V1 Replacement Readiness：保持现有路由，不自动合并 Draft PR。
Next shortest blocking path：后续授权部署 Oagnet 的三个运行文件后，重放原订单问题，核对金额字段、明细投影及 SQL，再验收订单结果。

## 后续授权部署：2026-09-23

用户随后明确要求部署并重启。本节更新上述部署前状态，不改变离线验证的证据边界。

- 功能版本：`528b553`；发布标识：`scalar-binding-528b553-20260923-174202`。
- 目标：49 的 `/root/yyy/Oagnet`，端口 `18022`。仅部署 `agent.py`、`api.py` 和新增的 `surface_literals.py`。
- 部署前两个已有文件与预期基线 `3dc237c` 一致，新增文件原先不存在；未发现他人改动冲突。完成 AST 检查、逐文件替换前二次漂移检查及部署后 SHA-256 核验。
- 备份与清单：`/root/.codex-deploy-backups/scalar-binding-528b553-20260923-174202`。替换过程配置文件回滚，实际未触发。

部署后运行文件 SHA-256（与本地版本库一致）：

| 文件 | SHA-256 |
| --- | --- |
| agent.py | 295f2c980fa9a0cba1d39ba44e661afb2ef123d9335ac0dc0e458bb221b55dc2 |
| api.py | 79880fbfbfa5a7ce94996822cd6681887817afef9d4eefa7de2ba78104d17139 |
| surface_literals.py | 4e844d4d3d0210310f21b93f42c24571c328b07915a884417f3ace612273a53e |

验证及重启：

- 使用远程实际部署代码执行标量约束与 source key 专项测试：**57 passed，2.31 秒**。向量库、MySQL、模型依赖为模拟对象；测试进程替换 logger，避免模拟结果写入生产应用日志。
- 仅重启 `oagnet-data-agent.service`：启动时间 **2026-09-23 17:42:47 CST**，PID 从 `3050419` 变为 `3148136`，状态 `active`，`NRestarts=0`。
- `data-analysis-agent.service` 保持 PID `3050444`、17:15:10 的启动时间；`sql-translator.service` 保持 PID `2051085`、2026-09-21 19:42:50 的启动时间。两者均未部署、未重启，状态均为 `active`。
- 从开发机检查 49：`18022/` 为 `UP`，`18022/vector/health` 的 success/healthy 均为 true，`8808/ready` 为 `READY`，所有检查与 readiness profiles 通过，无 degraded capabilities。
- 运行中的 `18022/openapi.json` 已包含 `QueryRequest.structured_extraction`，确认新 API 声明已加载。
- 本轮未重放真实业务订单问题；不能将模拟测试、接口健康和部署一致性等同于原订单查询已正确返回。下一步仍需核对真实 ASL 的金额条件、明细投影、SQL 与业务结果。

Current Stage：修复已部署，Oagnet 已重启，远程专项测试与健康检查通过。
V1 Replacement Readiness：保持 `V2_CONTEXT_V1_EXECUTION`，不切换路由，不自动合并 Draft PR；全局门禁仍未重新评估。
