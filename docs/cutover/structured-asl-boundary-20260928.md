# 结构化参数作为 ASL 唯一业务输入

## 合同与根因（PROVEN）

用户要求 ASL 只绑定规划节点的结构化参数，不再从原问题二次提取。
旧入口将结构化内容作为参考，随后调用原文重提取、粒度复核、条件补偿；
调用端又可消除指标/时间追问。因此规划、ASL 和实际执行可能偏离。

本次不切换 V2，不修改语义库或 SQL 服务，不改变六个主节点及正常展示顺序。

## 修复

- Oagnet 主入口只对 structured_extraction 逐参数召回，使用授权目录候选绑定。
  query/completed_question/retrieval_query 仅保留接口兼容，不参与模型输入。
- 删除原文生成链、独立粒度重提取、静默丢弃未匹配参考参数的函数，以及关系复核中的原文时间推断分支。
  不设置旧链回退开关；缺少结构化交接时明确报告规划节点缺项。
- 指标、分组、展示、条件、排序和限制由结构化参数控制；运算符和数值保留。
  标准值候选提供显式 ID，支持分开存放的表/列映射与完整逐参数召回池。
- 指标别名使用目录标准名称；空维度不自动补医院名称；未指定时间不加默认范围。
- 筛选归属、关系要求仍绑定已发布关系和实际字典键；不能判断时具体说明字段和值。
  已声明的共享科室关联、Top N 和可用展示字段保留。
- 必要参数绑定失败返回既有 ambiguity，调用端在确认执行合同之前处理，不能清除追问后运行 SQL。
  非关键展示字段部分缺失仍告知用户并返回其他字段，不把缺失条件静默删除。

## 文件清单

Oagnet：agent.py、structured_binding.py（新增）、query_binding_review.py。

DataAnalysis：app/adapters/surface_asl.py、app/adapters/http.py、app/services/asl_surface_handoff.py。
端到端补修：app/services/orchestrator.py、app/services/clarification_policy.py、
app/planning/task_dag.py、app/planning/structured_extraction_prompt.txt。

测试：Oagnet 的 structured_binding、scalar_metric_grain、query_binding_review、related_scope、
analysis_contract_request、administrative_defaults、metric_canonical_alias、partial_display_fields、
scope_closure、scope_contract、surface_detail_time、surface_evidence、surface_literal_constraints、
temporal_metric_invariants；DataAnalysis 的 surface_asl_client、http_adapters。

## 验证与测试迁移

- Oagnet 全量：1026 通过，无收集错误。
- SQL Translator 全量：491 通过（未改其业务代码）。
- DataAnalysis 关键适配/主节点顺序/多任务/Pending 测试：267 通过；随后增加 3 项 ASL 追问禁止执行 SQL 的入口测试。
- DataAnalysis 首轮全量：4324 通过、12 失败；其中 1 项旧断言要求消除 ASL 指标追问，
  按本次合同迁移后专项通过；另 11 项是已存在的 MCP 接口/设置改名问题，不修改无关模块。
- DataAnalysis 全量复跑：4325 通过、11 项上述旧失败，无新增失败或收集错误。
  此后新增的 3 项停止 SQL 流程测试单独通过（surface_asl_client 共 17 项通过）。
- STALE_TEST：旧测试要求 query-only 重提取、原文覆盖结构化粒度、清除时间/指标追问及静默丢字段，
  与新合同冲突，已替换为缺失交接、结构化形状、目录作用域、字面量和明确追问测试。
  Oagnet 基线 1039 项；新增 22 项，净退役/合并 35 项旧粒度复核用例，最终 1026 项。
- 服务器隔离只读试跑：医院总数保留空分组及空时间，上海绑定医院省份标准键；
  虚构指标明确提示缺项，不从审计原文偷换成医院总数；共享科室渠道排名保留关联范围和 Top 10。

## 发布状态

首版 c87e734 已逐文件部署并重启，配置哈希未改变；远程 Oagnet 122 项、DataAnalysis 270 项通过。
真实总数查询完成，空维度、空时间、单行结果及节点顺序通过；缺少结构化交接时明确报错。

复杂关系查询暴露三项衔接问题（PROVEN）：
- 目录逻辑分组与其身份名称列被当成两个不同分组。按 SQL Translator 已有身份/唯一名称投影规则识别等价，不新增分组；无关展示列仍明确提示。
- 旧追问门禁只接受至少两个候选，把明确缺项变成泛化 fallback。授权 ASL 返回的结构化缺项现允许开放式追问；服务问题保留具体说明且不执行 SQL，不虚构候选。
- 规划端英文任务意图和结构化中文意图不一致；输出要求的格式定义也与关系保留规则冲突。以结构化意图为准，并让输出要求保留不能由参数数组表达的关系片段；不恢复 ASL 原文重提取。

关联候选给模型提供显式编号及目标谓词，避免数组位置误选；修正后隔离试跑成功保留目标商品共同科室、医院限定、Top 10，分组名称重复展示不再误阻断。
补修离线 Oagnet 全量 1029 通过；DataAnalysis 关键链路 216 通过、规划/追问恢复 92 通过。
DataAnalysis 最终全量 4330 通过、11 项相同的 MCP 旧失败，无新增失败和收集错误；最后的追问阶段文案补充单独 5 项通过。

发布并行开发保护：发现服务编排和规划文件有同事新改动（对话文案、依赖展示、JSON围栏解析），首轮检查停止覆盖。
只在已核对哈希的服务器版本上应用本次不重叠补丁，保留其呈现模块；合并后远程 419 项通过，1 条旧展示断言导致自动回滚。
STALE_TEST：该断言要求意图识别重复显示拆分任务，与服务器已将任务列表放在规划节点的行为不符；改为核对补全问题，保留两个任务的规划、执行、结果及节点顺序断言。
部署不上传环境文件或凭据；保留 Draft PR，不自动合并。
已知无关阻塞：11 项旧 MCP 测试；本次不宣称 V2 全量替换就绪。
