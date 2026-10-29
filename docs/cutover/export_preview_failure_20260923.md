# 大结果附件失败时保留预览

## 当前目标与事实边界

修复 SQL 查询成功、附件导出失败后，智能体因全量行数超过内存阈值而丢弃查询结果的问题。

- PROVEN：SQL 大结果合同为超过 200 行导出完整 Excel，并提供前 20 行预览；旧异常分支却返回全部行、空链接和 `export_error`。
- PROVEN：HTTP 适配器此前没有消费 `export_error`；智能体在总行数超过 1000 且无附件时直接终止。
- PROVEN：只读排查曾发现当前文件存储的 bucket 检查返回 AccessDenied。
- UNKNOWN：未保留失败请求的原始导出异常，不能将 bucket 检查被拒绝直接等同于该次上传被拒绝。未进行真实上传或权限修改。

## 最小修复与清单

SQL Translator（提交 `c850cd4`）：

- `sql-translator/data_exporter.py`：大结果不论导出成功或失败，都保留 20 行预览、完整总行数与截断标记；Excel 的输入仍是完整结果。
- 导出失败继续保留 `success=true` 表示查询成功，附件地址为空；通过现有 `export_error` 和 `message` 说明附件失败。
- 权限/身份错误和缺失导出依赖提供可操作的分类提示；不向用户反射原始异常、内部 URL、凭据或业务值；日志记录异常类型和访问被拒分类。
- `sql-translator/test_sql_export_preview_failure.py`：新增 8 项边界与错误处理测试。

DataAnalysis Agent：

- `app/adapters/http.py`：消费并规范化现有 `export_error`；兼容旧 SQL 异常全量 JSON，压到 20 行且保留完整总行数。
- `app/domain/models.py`：增加内部 `result_export_error` 诊断字段，排除于序列化；不新增公共 API/SSE 字段。
- `app/services/orchestrator.py`：明确的导出失败返回 PARTIAL_SUCCESS、预览和附件失败说明；不运行全量分析、不生成虚假下载链接、不将预览保存成完整可复用数据集。
- `tests/test_export_preview_failure.py`：覆盖新旧 SQL 响应、错误脱敏、明细/趋势/报表、全量缺失的提示、完整文件成功路径、六节点流式顺序，以及没有导出失败证据时保持原内存保护。

正常小结果、正常附件、SQL 只读验证、授权 Semantic Scope、目录绑定、既有名单清理和其他模块不改动。没有放宽全量统计的完整性要求。

## 离线验证

- SQL baseline：455 passed；final：463 passed。新增 8 项通过，无旧通过变失败、无收集错误。
- DataAnalysis 专项（新测试、HTTP 适配器、分析编排）：168 passed。
- Critical Slice：146 passed，含新增附件成功/失败的 SSE 顺序断言、API、工作流、Pending、事件生命周期及 surface 链路。
- DataAnalysis 全量 baseline：4268 passed / 11 failed；final：4279 passed / 11 failed。失败用例集合完全相同，均为既有 `test_mcp_analysis_runner.py` 失败；没有 old-pass → new-fail 或 old-fail → new-pass，无收集错误。
- 全量收集包含 11 项新增测试；随后补充的无导出证据保护用例已在 168 项专项中通过。同步到版本仓后，12 项新增测试再次全部通过。
- MCP 旧失败涉及旧超时配置名 `mcp_analysis_tool_timeout_seconds` 等现有接口不一致，本轮不修改无关模块或旧断言。全量不是全绿。
- 测试使用模拟导出器、接口与会话，不上传生产文件、不执行真实业务 SQL。

## Review 与发布边界

- 预览不冒充全量：总数与已展示行数分开；全量统计、排名、趋势均不基于导出失败的预览推算。
- 成功路径测试确认依然展示前 20 行和真实返回的附件链接；失败路径无附件链接。
- 旧导出异常中的任意文本不进入用户输出；仅呈现限定的原因分类。
- 仅按上述清单同步版本仓；不包含环境文件、日志、缓存、生产数据或凭据。
- 当前阶段：结果交付容错修复；未部署或重启远程服务，未修改存储权限。
- 当前阻塞：线上完整附件能否恢复仍需排查真实导出失败原因和文件存储配置，离线通过不代表生产上传已恢复。
- Catalog/Evaluation/Shadow 全局门禁不重新评估；V1 Replacement Readiness 不变，无 V2 切流。
- 下一步：在获得部署授权后发布对应两服务，并分别验收正常下载与附件失败预览。不自动合并 Draft PR。
