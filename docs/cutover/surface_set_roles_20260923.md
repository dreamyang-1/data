# ASL 多值筛选与结构化角色修复

## 范围与证据

Current Stage：按用户要求修复 ASL 重复筛选，并接入用户提供的结构化提取语义说明；不推进 V2 切流。
基线：`25f13f3`；功能分支沿用 `fix/remote-chain-time-binding-clean-20260921` / Draft PR #85。

**PROVEN**：Oagnet 的 `_apply_surface_mention_normalization` 将 filter.value 整体转字符串与单词比较，识别不到 IN/NOT IN 中的成员，随后追加单值等号。SQL Translator 的平铺 filters 使用 AND，会错误收窄原来的可选值集合。上游把实体/指标/维度都传作带角色 mentions，而 Oagnet 原来不区分角色就查值目录，造成“商品”“含税销售总额”等被误报为缺失筛选。

通过实际函数、模拟目录进行离线复现：新加的 15 个 Oagnet 案例在旧函数上 14 failed / 1 passed，在新代码上全部通过。不是用修改旧断言制造通过。

## 最小修改

- Oagnet：IN/NOT IN 成员在原条件中更新，优先沿原组字段匹配；已经规范化的成员也识别为原集合的一部分。不额外追加等号，保留原操作符；仅去除完全相同的谓词。
- Oagnet：实体、指标、维度、返回字段及时间角色仍进入 ASL 语义参考，但不进入字面值匹配或“筛选未匹配”提示。明确标为筛选值的同名词仍正常匹配。
- DataAnalysis：`语义描述文件.md` 全文进入任务规划/结构化提取模型的 user message，补全后的问题独立放在最后；不覆盖平台角色设置，不改请求/SSE 格式。文档按调用读取，缺失时保留原提取路径。
- DataAnalysis：系统规则明确实体是逻辑对象/待关联表，而不是名称值；指标、维度、展示字段与具体筛选值分开。实际表关联和字段仍由当前授权语义目录绑定。
- DataAnalysis：新增非阻塞提示 `PRESERVE_SET_FILTER`，明确保留原条件以及可能漏匹配的风险；提示进入既有执行说明/结果通道。

用户说明原文保持不变，SHA-256：`e72c844ad3b5335070c790be375f92290092605bc9bd4162b4d0b0ea0ec9b22c`。
其中“商品科室关联”的若干经销关系描述与同文件实体定义不一致，已向用户指出；原文只是业务语义参考，不用于创建物理 JOIN 或授权。

## 跨字段边界

现有 ASL/SQL filters 是平铺 AND，不能表示跨字段 OR。本轮不扩展协议，也不修改 SQL Translator。
当 IN 里的词只能映射到其他字段，或未获得同字段标准值时，**保留原有 IN/NOT IN 条件并明确提示**，不拆成 AND、不随机丢掉成员，也不宣称该成员已完成标准化。原值不规范时仍可能漏匹配；后续由数据部门补齐同字段映射，或另行设计跨字段 OR 协议。最终字段仍经过现有授权目录校验。

## 回归

- Oagnet 专项：74 passed（后续增加两个已规范化成员案例，在最终全量中覆盖）。
- Oagnet 全量：792 passed；基线 777 passed；新增 15 项全部通过，无旧通过转失败。
- DataAnalysis 专项：76 passed。
- Critical Suite：216 passed，含候选确认、任务恢复、API/SSE 顺序、分析编排和新增提示词测试。
- DataAnalysis 全量：4224 passed / 11 failed，315.15 秒；基线 4220 passed / 11 failed。新增 4 项全通过；old-pass → new-fail = 0，old-fail → new-pass = 0，无最终 collection errors。剩余同一批 11 项 MCP 配置/方法接口不一致旧失败，未改同事模块。
- 没有放宽旧测试、跳过业务断言或改动数据库。开发目录直接递归收集 Oagnet 时曾误收 `_backups` 中旧测试导致 4 项 collection errors；改从版本仓指定 `tests` 目录后最终无 collection errors，未修改测试或正式代码绕过。

## 同步清单与审查

按明确文件清单同步到版本仓，逐一核对 SHA-256，不复制环境、日志、缓存、备份或凭据：

Oagnet：`agent.py`、`surface_evidence.py`、`tests/test_surface_set_filter_roles.py`。

DataAnalysis：`app/planning/task_dag.py`、`app/adapters/asl_notices.py`、`tests/test_extraction_semantic_description.py`、`语义描述文件.md`、本报告。

自审：原位更新而非新增限制；IN/NOT IN 均有正反例；完全相同谓词去重不合并不同操作符；角色词与同名实际值有反例；保留现有用户节点和授权边界。不触及同事 MCP 模块或其他业务实现。

Oagnet 独立提交：`6915444`。DataAnalysis 和本报告在后续独立提交中发布，均留在功能分支，不自动合并。

Cutover Blocker P0/P1、Catalog/Evaluation/Shadow Gap：本轮只验证上述局部改动，不重新认定全局切流条件。
V1 Replacement Readiness：不切流、不自动合并、不宣称生产就绪。
Next shortest blocking path：如需上线，同步上述运行文件及语义文档，按当前远程版本检查差异后部署并做真实请求验证。本轮没有部署或重启远程。
