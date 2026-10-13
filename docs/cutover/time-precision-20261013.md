# 时间范围与分组粒度闭环（2026-10-13）

## 根因与修复范围

PROVEN：平台旧 `structured_time.bounds` 能处理 `2025年8月`，不能处理
`2025年8月份`；不是旧版本回退。同时 ASL 校验没有分钟、秒、半年，
SQL 分组缺少分钟、秒、半年，范围 SQL 的结束边界只按日期加一天。
本次只修改结构化提取 → 时间绑定 → ASL 校验 → SQL 时间范围/分组链。
不更改实体关系、业务域授权、Pending、V2 路由、主节点或数据库配置。

- 九种自然粒度：秒、分钟、小时、日、ISO 周、月、季度、半年、年。
- 月份/年度/号、中英文粒度别名、季度、半年度、绝对 ISO 周、相对及滚动时间。
- ISO/中文日期时间，秒/分钟/小时窗口，明确区间与排除上界，跨年/闰日。
- 范围不自动产生分组，详情时间列不截断；模型不能改写已提取的范围或粒度。
- 同一次绑定固定真实时钟；细粒度相对范围不能以日期零点代替当前时刻。
- 日期区间保持原闭日语义，SQL 上界加一天；时间区间闭秒，上界加一秒。
  使用半开 WHERE 区间包含最后一秒内的小数记录，但排除下一秒/分钟/日期。
- 保留用户明确的发货日期等业务 anchor，由授权 ASL 绑定物理字段。
- DATE-only 字段不能凭空生成时分秒。财年、工作日、自定义周期需业务定义；
  不自动猜测时区，ASL 不接收毫秒/微秒范围或时区偏移的新格式。
- `上半年/下半年` 指本年命名半年度；`上一半年/下一半年` 指相邻半年度。

## 明确变更清单

开发目录逐文件 SHA-256 同步到版本仓，无目录覆盖、凭据或生产结果提交：

- `Oagnet/structured_time.py`
- `Oagnet/structured_binding.py`
- `Oagnet/agent.py`
- `Oagnet/tests/test_time_precision.py`
- `Oagnet/tests/test_surface_detail_time.py`
- `sql-translator/sql_translator_prod.py`
- `sql-translator/test_time_precision.py`
- `app/planning/structured_extraction_prompt.txt`
- `tests/test_time_precision_prompt.py`
- 本报告

旧明细时间测试只增加 datetime 时钟替身，原断言不改。原因：细粒度需要
真实时刻，原测试仅替换 date.today，无法冻结新增 datetime.now；该替身也
兼容旧实现。修改前后所有旧测试结果一致，不用更改业务期待消除失败。

## 离线验证与 Delta

| 服务 | Baseline | Final | 新增通过 | 原通过→失败 |
| --- | --- | --- | --- | --- |
| Oagnet | 1674 pass / 24 fail | 1797 pass / 24 fail | 123 | 0 |
| SQL Translator | 644 pass / 15 fail | 666 pass / 15 fail | 22 | 0 |
| DataAnalysis Agent | 4777 pass / 124 fail / 6 collection errors | 4778 pass / 124 fail / 同 6 collection errors | 1 | 0 |

Critical Suite + ASL 节点顺序 + 新提示词测试：19 passed。
Agent、Oagnet 逐节点比较全部旧结果一致；SQL 全部失败节点集合一致。
现有 Legacy 失败和 Agent 收集错误没有宣称清零，不在本次最小调用链内。
新增 146 项测试全部通过。Review 核对范围/分组分离、DATE 精度、日期/秒
上界、ISO 周年边界、命名半年含义、冻结时钟、Scope 字段绑定和旧行为。

## 49 发布与真实验证

以修改前版本为基线生成 hunks，零 fuzz 合并到远程当前文件，保留远程
提示词其他改动。五个运行文件备份并验证并发差异，配置和无关代码哈希不变。
第一次发布验收使用错误的通用健康路径，已自动回滚；改用 Oagnet 实际
`/vector/health` 后重新完成候选测试和发布。三个服务已重启且健康检查通过。

远程候选测试：Agent 155→156 passed、相同 20 个旧失败；Oagnet 148→271
passed；SQL 52→74 passed（均另有 3 subtests passed）。无新增失败或收集错误。

真实 MySQL 只读测试 12 项全部通过：九种粒度分别对四条固定日期时间
常量进行 GROUP BY，再检查月份/分钟/秒三种范围的含尾排首边界。
含 `23:59:59.999999`、ISO 周跨年和下一秒排除；不是生产业务数据写入，
也没有把纯函数 Mock 称作真实 SQL 执行。

平台端到端：年度上海医院订单数量查询 COMPLETED，表格存在，七个公共
节点顺序正确。截图原问题「2025年8月份…金额最多的销售员」已通过时间
解析并真实执行 SQL：ASL 绑定 `sales_order.created_date`，范围
`2025-08-01`～`2025-08-31`，SQL 条件 `>= '2025-08-01' AND < '2025-09-01'`。
返回 0 条，已不再发生时间粒度追问或执行失败。医院名称筛选绑定证据不完整，
不能推断业务本来无数据，该名称匹配需要独立核实，不扩大本次时间链修改。
原生空结果快路径也未展示结果校验/洞察节点；本次没有改动此既有编排路径，
未将空结果样例宣称为七节点全部通过。

模型 126/[292] 发货日期查询被目录加载拦截：
`CATALOG_STABLE_ID_REQUIRED`，「最终客户维度」的
`semantic_model_dimension.code` 为空。尚未进入时间解析或 SQL，不能声称
该业务查询通过。本次不修改语义库、不猜维度编码、不扩大授权。

## 工程阶段边界

Current Stage：当前用户时间粒度修复；未推进 V2 切流。
本次时间链缺口已闭环；模型 126 配置阻塞需语义模型维护方处理。
Catalog/Evaluation/Shadow 和 V1 Replacement Readiness 未因本次修复改变。
Next shortest path：补齐真实缺失的维度编码后重新只读验证发货日期查询。
