# 近期改动部署与重启记录

## 发布范围

- 用户明确要求部署近期改动并重启。发布代码基线：`09c1086`。
- 发布批次：`recent-09c1086-20260924-102543`，远端已保存原文件和 manifest，可按文件回滚。
- 预检的 7 个目标文件全部匹配 Git 中已知旧版本，无未知远端漂移；只上传明确文件，未整目录覆盖或更新环境配置。
- 已逐文件核验部署后的 SHA-256 与本地已提交代码一致。

| 服务 | 运行文件 | 涉及功能提交 |
| --- | --- | --- |
| SQL Translator | data_exporter.py、sql_translator_prod.py | c850cd4、0c16d6e |
| Oagnet | agent.py、prompt_build.py | 7fe9e94、09c1086 |
| DataAnalysis | app/adapters/http.py、app/domain/models.py、app/services/orchestrator.py | dc4ca93 |

覆盖大结果附件失败的 20 条预览兜底、同名行政层级细粒度默认、指标标准名称及 SQL SELECT/ORDER BY 列名一致性。部署前现场读取了配置文件哈希，发布后完全一致；此前修复的 MinIO 配置未被覆盖。

## 远端验证

先在已部署源文件上运行 Mock 专项，再按 SQL → Oagnet → DataAnalysis 顺序重启。

- SQL 专项：20 passed，覆盖导出阈值/失败预览与标准列名。
- Oagnet 专项：51 passed，覆盖行政层级默认与标准指标名称。
- DataAnalysis 专项：12 passed，覆盖预览兜底、结果状态与事件顺序；1 条既有 Starlette 测试客户端弃用警告。
- 共 83 项通过，无测试失败。

| 服务 | 重启前 PID | 重启后 PID | 启动时间（CST） | 健康状态 |
| --- | --- | --- | --- | --- |
| SQL Translator | 560012 | 2470678 | 2026-09-24 10:25:59 | HTTP 200 / ok |
| Oagnet | 3824713 | 2470735 | 2026-09-24 10:26:01 | HTTP 200 / UP |
| DataAnalysis | 3825061 | 2471012 | 2026-09-24 10:26:05 | HTTP 200 / READY |

三项服务 ActiveState=active，NRestarts=0；向量健康 success=true。后续冒烟时再次核验代码和进程状态，无再次重启或漂移。

## 真实接口冒烟与更正

- 对已授权模型/业务域，以“查询上海地区医院总数”及对应结构化参考调用真实 ASL 接口。
- 最终复测：HTTP 200，约 7.7 秒；无 ambiguity；返回 alias 为“区域全部医院总数”。
- PROVEN：线上本次选中的标准 code 是 `total_hospital_count_by_region`；semantic_evidence 的 canonical_code/canonical_name 与 ASL 一致，sql_verified=true，metadata_source=MYSQL_SEMANTIC_LAYER。
- 首次冒烟使用旧截图 code `total_hospitals_in_region` 做固定断言，因当前目录 code 不同而失败；不是服务错误。将探针改为核对真实返回的语义证据后通过。没有为适配测试去修改目录、生产代码或指标绑定。
- 将同一 ASL 发往真实 SQL 翻译接口，HTTP 200、success=true，SQL 输出列名包含“区域全部医院总数”。仅翻译，未执行业务 SQL，未取业务结果行。
- 当前验证不等同于完整平台 SSE/业务结果验收，也未新建导出附件进行存储写入；导出成功路径沿用已验证且本次哈希未变的配置，异常路径已由远端 Mock 覆盖。

## 状态

- Current Stage：本次 7 文件发布、三服务重启、健康/向量检查、远端专项和真实 ASL→SQL 翻译冒烟完成。
- 本项新增 P0/P1：无。Known blockers：上次记录的 12 项严格离线旧测试替身缺口仍在，不影响本次已通过的部署专项。
- Catalog/Evaluation/Shadow Gap 与 V1 Replacement Readiness 不变；本次没有目录发布、V2 切流或自动合并。
- Next shortest blocking path：用户在平台发起新请求验收实际结果；已保存的历史消息不会因后端更新而自动改写。
