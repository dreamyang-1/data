# 指标标准名称贯穿 ASL 与 SQL 输出

## 根因与边界

- PROVEN：Oagnet 的 SYSTEM_PROMPT 明确要求 metrics.alias 填用户原话；普通查询最终校验仅约束 name 属于已召回 metric_code，没有统一 alias。因此指标编码正确时仍可显示用户简称。这不能单凭截图判定为向量匹配失败。
- PROVEN：SQL SELECT 以前保留公式末尾的反引号 AS 别名，即使 ASL 提供了另一个 alias；ORDER BY 却使用 ASL alias，两者可能不一致。
- 截图中的 `total_hospitals_in_region` 与“区域全部医院总数”配对来自用户提供的说明，已用该配对做回归；本轮未访问线上向量目录，未核实实际部署版本或本次请求轨迹，不宣称已线上复现。
- 范围：仅指标显示名称，不改变指标选择、计算口径、关系、筛选、分组或排序方向；不修改 DataAnalysis 的结构化提取、Schema、SSE、Semantic Scope 或授权。

## 新合同与修复

- metrics.name 保持当前召回内的 metric_code；最后返回前，metrics.alias 从同一 code 的 metric_name 回填，不允许模型缩写或使用用户原话。未召回编码仍由既有校验拒绝，不能因 alias 看起来正确获得授权。
- 目录缺少 metric_name 时回退到已注册 code，不新增阻断。使用既有 `_metric_vector_metadata`，与既有指标证据读取记录的选择保持一致。
- 统一普通调用、带证据调用、调用方已绑定指标和探索流程的最终显示名称；不改输入接口。
- 用户本次要求覆盖旧的“alias=用户原话”和“公式带 AS 时模型传 null”提示约定：Oagnet 现在统一输出标准名，由 SQL 翻译器处理公式旧列名。
- SQL 在 ASL 指定 alias 时，仅移除计算表达式末尾的输出列别名，添加 ASL 列名；不触及 `CAST(... AS type)`。SELECT 与指标 ORDER BY 对反引号一致转义。旧调用方 alias=None 的 SELECT 行为不变，不强制其他调用方生成名称。
- 最终形态：`{"name":"total_hospitals_in_region","alias":"区域全部医院总数"}`。中文名称不替代指标编码。

## Change manifest

Oagnet：

- `Oagnet/agent.py`：新增最终指标别名回填并接入 `_validate_asl_output` 返回前。
- `Oagnet/prompt_build.py`：标准名称规则替代用户原话/公式别名规则。
- `Oagnet/tests/test_metric_canonical_alias.py`：19 项测试。

SQL Translator：

- `sql-translator/sql_translator_prod.py`：SELECT 输出别名与 ORDER BY 一致。
- `sql-translator/test_metric_canonical_output_alias.py`：12 项测试；只模拟目录访问，计算公式解析与 SELECT/ORDER BY 构造使用真实代码。

## 回归及 Review

| 检查 | baseline | final |
| --- | --- | --- |
| Oagnet，全量既有数据库/向量 Mock 运行器 | 922 passed | 941 passed |
| Oagnet，仓库严格离线运行器 | 910 passed / 12 failed | 929 passed / 相同 12 failed |
| SQL Translator，完整离线回归 | 463 passed | 475 passed |
| Oagnet 标准名/时间/语义证据/hardening 专项 | — | 74 passed |
| 上游 Critical Slice | — | 206 passed |

- 两服务严格离线报告逐用例比较：old-pass → new-fail = 0，old-fail → new-pass = 0；新增 Oagnet 19 / SQL 12；无 collection error，无旧断言修改。
- Oagnet 12 项旧失败与前一轮完全相同，属于未补齐存储/数据库替身；本轮不顺带修改。详见同目录 `administrative_finest_default_20260924.md`。
- Critical Slice：question_rewriter、pending_execution_transition、phase0b_critical、api、workflow、bridge_event_lifecycle_contract、bridge_event_lifecycle_wiring、asl_surface_handoff。
- Review：模型改写/null/缺 alias 均回填标准名；缺目录名不新增阻断；假编码保持拒绝；公式已有各种 AS、CAST、旧 null 调用和反引号列名均覆盖。未执行生产 SQL、修改配置、重启或部署。

## 发布与状态

- 按服务分别提交。发布需包含 SQL Translator 与 Oagnet，建议先更新 SQL 的别名兼容处理，再更新 Oagnet；只部署 Oagnet 可能在带旧公式别名的排序查询上仍出现列名不一致。
- Current Stage：本项代码及离线验证完成，进入分支评审；本项无新增 P0/P1。Known blockers：12 项旧测试替身缺口；真实目录与线上请求效果尚未复测。
- Catalog/Evaluation/Shadow Gap、V1 Replacement Readiness 不变，本轮不构成 V2 替代就绪证据。
- Next shortest blocking path：用户要求部署后按上述顺序发布，复测医院总数及带指标排序的查询；不自动合并 PR。
