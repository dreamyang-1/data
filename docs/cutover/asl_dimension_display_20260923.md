# ASL dimensions 展示用途说明

## 范围与证据

- PROVEN：页面中的 ASL Markdown 来自 `app/presentation/intent_recognition.py::render_asl_extraction_json`，HTTP 与 mock 适配器均使用该展示函数；不是前端单独定义的 JSON 字段名。
- PROVEN：SQL Translator 用 `not metrics and bool(dimensions)` 判断明细投影；明细投影不生成 GROUP BY。展示说明采用同一判断，不根据金额、日期等字段名称猜用途。
- 本次仅调整页面展示文案。英文标签为 `dimensions / display_fields`；下方根据实际 ASL 解释使用展示字段还是分组维度，空字段列表明确说明未使用。不重命名 JSON 的 `dimensions`，不添加执行字段，不改变请求、响应/SSE 结构、ASL、SQL、Scope 或工作流节点。
- 展示标签位于 JSON 下方，避免让用户把展示别名误当成可执行协议键。原 filters 说明保留。

## 文件与自审

- `app/presentation/intent_recognition.py`：英文标签、明细/汇总/空列表三种说明。
- `tests/test_intent_recognition_display.py`：新增 8 项，涵盖明细金额、名称、按地区/月份汇总、空/null/缺省；断言原对象和展示 JSON 不变，保留全部旧断言。
- 本记录。按清单同步至版本仓并核对 SHA-256，不同步日志、配置、凭据或其他模块的修改。
- Code Review：无模型调用、无查询重写、无新增协议字段、无节点或时序变更。执行 JSON 的原样渲染合同保持不变。运行时代码仅一个展示函数有变化。

## 验证

- 修改前展示专项：15 passed；修改后：23 passed，新增 8 项，无旧断言修改。
- 展示与 Critical Slice：182 passed，含 API/SSE 顺序、候选执行恢复、事件生命周期、分析编排、surface 交接与工作流。
- 全量基线引用同一工作区上一功能提交的记录：4253 passed / 11 failed，旧失败均位于 `test_mcp_analysis_runner.py`。本次最终结果：4261 passed / 11 failed，443.73 秒；失败节点集合逐项比对完全一致，新增 8 项通过，old-pass → new-fail 为 0，old-fail → new-pass 为 0，无 collection errors。不因本次展示任务修改 MCP 的旧失败。
- Draft PR #85 已确认 open/draft，功能分支 `fix/remote-chain-time-binding-clean-20260921`；不自动合并。
- 本轮未部署或重启，不宣称线上已生效；无真实业务查询验收。

## 阶段

Current Stage：页面展示文案修复完成，专项、Critical Slice 及全量差异核验完成。
Cutover Blocker P0/P1、Catalog/Evaluation/Shadow Gap：本次不重新评估全局门禁；保持历史已知问题状态。
V1 Replacement Readiness：不变，不切换生产路由。
Next shortest blocking path：后续授权部署后验收页面标签与用途说明；本轮未改变线上服务。

## 后续授权部署与重启

用户随后明确要求部署重启，以下记录更新前述未部署状态。

- 功能版本 `1151d5d`；发布标识 `dimension-display-1151d5d-20260923-191029`。
- 仅更新 DataAnalysis 的 `app/presentation/intent_recognition.py`。远程旧文件与 `32ed361` 基线一致（忽略 CRLF/LF 差异），替换前再次核对原始字节，未发现他人修改冲突。
- 备份目录 `/root/.codex-deploy-backups/dimension-display-1151d5d-20260923-191029`，保存旧文件和哈希清单；上传后 AST 与哈希验证通过，原子替换，未触发回滚。
- 远程运行文件与版本仓 SHA-256 一致：`8b29d130b9cdee4e0ab238e9b470b6b8504e5416e93ac1d41f30d9fa575e95dd`。
- 使用远程部署源码执行展示专项：**23 passed，0.61 秒**。测试放在临时目录，未覆盖线上测试文件。
- 仅重启 `data-analysis-agent.service`：**2026-09-23 19:10:45 CST**，PID `3050444` → `3476328`，状态 `active`，`NRestarts=0`。
- Oagnet 保持 PID `3148136`，SQL Translator 保持 PID `2051085`，状态与启动时间均未变。
- 服务本机及开发机访问平台 `/ready` 均为 `READY`，readiness profiles 全部通过，无 degraded capabilities；运行模式仍为 `V2_CONTEXT_V1_EXECUTION`。
- 本次验证部署版本、展示逻辑和健康状态，未发起真实业务查询或浏览器视觉验收。历史会话内容不会被回写；新请求生成的 ASL 展示使用新说明。

Current Stage：展示说明已部署，目标服务已重启，远程专项及健康检查通过。
V1 Replacement Readiness、Cutover/Catalog/Evaluation/Shadow 全局门禁不变；不切换路由、不自动合并 Draft PR。
Next shortest blocking path：新建查询验收页面中的英文标签及本次用途说明。
