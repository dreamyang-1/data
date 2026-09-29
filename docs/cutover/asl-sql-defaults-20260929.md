# ASL 与 SQL 执行默认规则对齐

PROVEN：SQL既有明细默认LIMIT和实体主名称非空分组过滤不在ASL用户条件中，
原展示只说用户上限与指标固定口径，未说明这两种补充，容易被误认为条件不一致。

本次保持原查询行为，不删非空组过滤，不取消明细行数保护，不新增门禁。
SQL构建和既有effective_filter_summary使用同一默认规则函数：在摘要里追加
system_filters、effective_limit、limit_source，分别标注具体条件、影响及来源。
API地址、请求参数、ASL协议及原响应字段均不变，旧调用方可忽略新增摘要项。
页面保留原ASL用户条件，调度节点单列实际执行上限和系统补充筛选；缺少服务端
摘要时明确说未提供，不猜测为无限制/无补充。补充展示ASL时间和HAVING。
不改指标口径、授权、SQL只读约束、关联关系、业务结果及七主节点顺序。

清单：sql-translator/sql_translator_prod.py；
app/adapters/http.py、app/presentation/execution_trace.py、app/presentation/intent_recognition.py；
新增两端test_execution_defaults_alignment.py / test_sql_execution_defaults_display.py。

SQL全量510通过（新增11项）；数据分析相关173项通过（新增7项）。
全量与部署结果待补记；上一轮数据分析基线4393通过、11项旧MCP失败。
V1展示与执行来源说明修复；不涉及V2切流、目录/语义配置或索引变更。
