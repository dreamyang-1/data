# 可选展示字段缺失时继续返回可用结果

## 根因及证据

- PROVEN（代码与离线复现）：Oagnet 的未召回展示字段规范化可将无效字段移出 dimensions，同时增加 dimension ambiguity；DataAnalysis 的 surface 客户端见 ambiguity 会抛 ASL_AMBIGUOUS，编排请求澄清。当该目录问题不适合向用户追问时，最终输出“当前语义目录或执行服务尚未提供足够依据，系统无法安全继续处理”。
- PROVEN（只读远程日志）：当日 12:00 的相关请求有 fields=邮箱/地址 未匹配记录，最终输出投影仅 dealer.dealer_name，ASL 校验通过。旧的 DROP_UNVALIDATED_STRUCTURED_REFERENCE 只是参考项省略记录，不是经最终 ASL 确认的缺列提示，因此不能直接作为展示缺失判据。
- UNKNOWN：截图没有请求 ID，现存日志没有记录该截图请求的完整 ambiguity；上述日志不能作为截图那一次响应的一一对应证明。
- PROVEN（真实分类器离线检查）：明细请求的 dimensions 可能包含商品、经销商等对象提示，fields 则包含经销商名称、邮箱、地址。不能把非空 dimensions 一概当成统计分组并禁用此次降级。

## 最小修复

Oagnet 在 surface advisory 详情查询的字段补召回之后、最终向量及 ASL 校验之前处理部分展示：

- 至少一项用户请求的字段已匹配到当前授权投影时，才允许省略其他无法绑定的展示字段。
- 字段存在但本次没有完成选定/绑定，同样可以只返回其余字段；不随机补列、不编造电话等内容。
- 可识别的缺列 ambiguity 转为 OMIT_UNAVAILABLE_DISPLAY_FIELD 审计记录；所有剩余字段继续通过向量范围及 ASL 校验。
- 没有可用请求字段时不拿无关名称顶替。指标/聚合、明确分组、时间粒度以及缺失字段参与过滤、排序、having、时间锚点时不按可选展示处理。
- 其他实体、筛选、时间、指标或无法识别的 ambiguity 保留；有 caller-owned intent contract 的原路径不变。
- 已有联系方式展开为授权邮箱/地址的成功行为保持不变。

DataAnalysis 只负责消费最终缺列记录，通过现有执行提示、审计、可靠性说明和最终回答通道显示：

> “联系方式”展示字段未匹配到本次可用的标准字段，无法显示经销商联系方式；已继续返回其他可用字段。此提示不表示数据库中一定没有该信息，请数据部门核对字段配置。

不改请求或 SSE 结构，不修改用户可见主节点顺序，不改结构化提取、workflow、SQL Translator、数据库或权限。

## 文件与验证

- Oagnet：`agent.py`、`tests/test_partial_display_fields.py`。
- DataAnalysis：`app/adapters/asl_notices.py`、`tests/test_asl_binding_notices.py`、本报告。
- Oagnet 基线 792 passed；最终全量 812 passed（新增 20 项），无旧通过转失败，未修改旧断言。
- DataAnalysis 新增 3 项提示/完整编排 Mock 测试；关键测试 269 passed，含节点顺序、候选恢复、surface 客户端、名单清理与原有接口回归。
- DataAnalysis 最近全量基线 4241 passed / 11 个 MCP 旧失败，另有前一轮追加的名单预览保护用例已在关键测试通过；本轮全量 4245 passed / 11 failed，389.10 秒。失败集合与基线一致；无 collection errors，无旧通过转新失败，旧失败转通过 0。本轮新增 3 项及前轮追加的 1 项全通过，无旧断言修改。
- Code Review：依据最终选择投影而非前置 dropped reference 判缺列；确认生成入口实际调用、保留正常筛选、原有联系方式展开、字段全缺失和同字段参与约束的反例。未扩大授权，未碰其他模块或降低 SQL 安全检查。
- 明确文件清单逐一同步到版本仓并比对 SHA-256，不复制环境、凭据、生产记录或日志。
- Oagnet 独立提交 `1c81b92`；DataAnalysis 提示消费与本报告另行提交，同步至现有功能分支及 Draft PR #85，不自动合并。

## 状态与边界

Current Stage：本次缺列继续输出修复，离线验证与发布分支收尾；本轮尚未部署生产。
Cutover Blocker P0/P1、Catalog/Evaluation/Shadow Gap：不重新评定既有全局门禁；不以 Mock 替代生产业务验证。
V1 Replacement Readiness：不切换 V2、不自动合并。
Next shortest blocking path：按单独部署授权发布两个运行文件，再用新会话验收缺联系方式的名单查询。

## 后续授权部署（2026-09-23）

用户随后明确要求“部署重启”。发布 `partial-fields-4177bb8-20260923-131535` 只更新两个运行文件：

- Oagnet `agent.py`，SHA-256：`b2ac88a7a5239c07fefab3a7c5ad001ed2d306abed4e023332b5886115a79ba1`。
- DataAnalysis `app/adapters/asl_notices.py`，SHA-256：`a671e096ede701f18fa853673297fc47602340aab915769b2237fa770e4cfd69`。

发布前两份远程文件均与基线 `67ed996` 一致。执行语法检查、逐文件备份、写入前漂移检查、原子替换和发布后哈希核验，未覆盖其他模块、环境配置或生产测试目录。回滚文件与清单位于远程受限备份目录，可按发布标识定位；回滚前需确认无后续文件更新。

远程临时测试目录加载实际部署源码：Oagnet 部分字段输出专项 20 passed（2.75 秒），DataAnalysis 缺列提示及编排测试 6 passed（0.70 秒）；数据、目录、模型调用使用 Mock。

- Oagnet 服务于 13:16:31 CST 重启，PID 1774399 → 2283494。
- 数据分析服务于 13:16:32 CST 重启，PID 1958059 → 2283529。
- 两者 active/running，NRestarts=0。服务器内及开发机访问 18022 `/` 返回 UP，8808 `/ready` 返回 READY、所有 profiles 通过、无降级项；18022 `/vector/health` healthy=true。
- SQL Translator、数据库、索引和其他模块未改动、未重启。未运行真实业务问题，不将 Mock 或健康检查当成业务答案正确性的证明。

Current Stage：本次可选展示字段降级已部署并重启，等待用户新会话验收。原有切流门禁与 V1 Replacement Readiness 不变，不自动合并 PR。
