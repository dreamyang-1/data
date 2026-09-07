# 测试与评测框架缺口

## 已有能力

- 两套现有意图样本共200条，可作为旧单标签意图的部分金标。
- 有30条真实历史失败样本及业务矩阵、医疗场景、60题和多轮专项脚本。
- pytest覆盖意图、对话、澄清、状态、适配器、数据集、Oagnet和SQL翻译器。

## 本轮结果

- DataAnalysis核心集：242通过、7失败。
- DataAnalysis扩展语义/多轮集：725通过、18失败。
- Oagnet安全隔离单测：185通过。
- SQL Translator单测：101通过。
- 另有两组测试在收集阶段失败：`BusinessRuleRef` 与 `ResultValidationReport` 已不存在，说明测试/Schema漂移。

## 缺失指标

现有accuracy和Macro-F1不足以覆盖多轮数据智能体。必须增加对话动作、服务路由、槽位角色、规范项Top1、召回率@K、逻辑计划精确匹配、结果列/粒度/顺序正确率、澄清精确率、首错层定位率、状态隔离违规率。定义见 `evaluation_metric_spec.json`。

## 数据要求

- `gold_cases_seed.jsonl` 的200条是现有单标签断言迁移种子，标为 `PARTIAL_GOLD_EXISTING_ASSERTION`，不是多轴完整金标。
- `synthetic_test_proposals.jsonl` 明确标记 `synthetic_proposal=true`、`ground_truth=false`。
- 新金标必须冻结目录版本、数据快照、会话历史、每轮预期计划及结果契约。

