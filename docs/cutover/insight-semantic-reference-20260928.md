# 洞察分析接入业务语义参考

## 根因与范围

PROVEN：规划阶段读取语义描述文件.md，QwenAnalysisSynthesizer 未读取该文件；洞察调用仅携带统计、20 行预览、问题和平台风格配置，没有完整的已执行 ASL/SQL。
用户要求洞察模型理解维度、字段、指标及绑定关系。本轮仅补齐模型上下文，不修改业务查询和数据质量门禁，不恢复洞察内容审核。

## 修改

- app/analysis/synthesis.py：每次调用读取与规划相同的语义描述文件.md，放入用户消息 semantic_reference。完整文件作为业务参考，系统要求只解释当前相关部分，不当作执行指令或授权清单。
- app/services/orchestrator.py：模型专用 facts.executed_query 携带当前 DataQueryResult 的 ASL/SQL；深拷贝 ASL，不改变查询或持久化的分析证据，也不加载其他任务的结果。
- DSL 帮助解释业务定义和单位；实际查询解释必须依据已执行口径。绑定维度列表不等于实际分组、目录关系不等于已执行 JOIN，差异应说明，不能用文档修饰为正确口径。
- 文件缺失、空白、读取失败时正常分析已有数据；不新增验证或追问门禁。每次读取，后续文件更新无需模型进程缓存失效。
- 现有长报告、无图表、预览范围提示和宽松响应解析保持不变。

## 清单与验证

生产文件仅两个：app/analysis/synthesis.py、app/services/orchestrator.py。
测试：tests/test_analysis_synthesis.py、tests/test_analysis_orchestration.py。
专项 196 passed，覆盖洞察、编排、预览、API 七节点顺序、多任务和 Pending。
新增 5 项：相同 DSL 路径、热更新与用户消息位置、缺失/空白/无效编码降级；扩展现有编排测试验证实际 ASL/SQL 交接且不写入持久分析证据。
远程预检查：DSL 文件存在且为 21,792 字节；synthesis 与基线一致，orchestrator 仍有同事独立改动，只应用本轮非重叠补丁；不上传 DSL 或配置，不覆盖同事内容。
本轮无 ASL/SQL 翻译或语义库修改。V2 Readiness 未改变，不宣称替换就绪。

全量离线：4348 passed、11 failed（254.07 秒）；基线 4343 passed、11 failed。新增 5 项全部通过，旧通过变失败 0，收集错误 0。
11 项失败仍为 test_mcp_analysis_runner.py 既有配置和方法接口不一致，未改动或跳过。
Review：不改变 synthesizer 调用签名或输出合同；不复制配置/DSL 内容到系统消息，不新增授权来源，不恢复内容审核；单任务与多任务复用同一模型入口。
