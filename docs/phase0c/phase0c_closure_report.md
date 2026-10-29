# Phase 0C Trust & Semantic Scope Closure

状态：DataAnalysis 侧范围约束已实现；安全断言通过；完整 `SEMANTIC_SCOPE_CONTRACT = BLOCKED_EXTERNAL_CONTRACT`。不能宣称生产就绪。

## 版本与发布

- 开发/测试目录：`E:/YouoAgent/DataAnalysis_Agent`。
- Git 仓库：`E:/yy`。
- 基线：`3b3d60ccdd2698887ba57e2b849564861adf7e7c`，Phase 0B。
- 分支：`phase0c-trust-semantic-scope-20260908-01`。
- 功能提交：`1c28cd64e0422c98ad4bb3b0222701f63845984f`。
- 关闭证据提交：包含本报告的提交；使用 `git log -1 --format=%H -- docs/phase0c/phase0c_closure_report.md` 精确解析，避免将提交自身哈希写入自身造成循环引用。
- GitHub Draft PR：[PR #3](https://github.com/dreamyang-1/data/pull/3)，base 为 Phase 0B 分支。未自动合并。

## 实现结果

业务后端负责认证和授权；DataAnalysis 仅约束每轮当前后端传入的 AuthorizedSemanticScope。模型 ID 改为严格正整数必传；非法或缺失范围直接返回 REQUEST_SCOPE_INVALID。兼容单 business_domain_id，内部统一排序去重后的 business_domain_ids，显式冲突直接拒绝。内部使用 MODEL_WIDE / EXPLICIT_DOMAINS。

复用现有业务后端 Bearer authKey 方式校验调用服务。tenant/user 仅作为状态命名空间，已删除默认身份兜底。未配置服务令牌、缺少身份标头或应用标识冲突均拒绝请求；未在本地 .env 中添加凭据。部署方需要配置 `DATA_AGENT_TRUSTED_BACKEND_TOKEN` 并转发可信状态标头。

Pending、Task Frame、Last Request、历史任务引用和 DAG 分支推广均核对当前范围。旧状态不能经 Last Request 回退重新进入。DAG 恢复与检查点、响应缓存、ASL 缓存、知识缓存、Dataset、报告产物和已验证语义召回均纳入范围指纹。显式 Dataset ID 也不能绕过检查。当前请求不从历史、模型或 Pending 继承授权。

## 外部接口实证与最高阻塞

只看 Oagnet 接口 Schema 会得出“支持多域数组”的结论。继续读实际 `prompt_build.py::PromptBuilder._build_where` 后发现，它在显式集合中额外加入 `-1`。因此当前主查询接口不满足严格集合合同，单域也受影响。

DataAnalysis 已在调用前 fail closed：单域返回 EXPLICIT_DOMAIN_NOT_SUPPORTED，多域返回 EXPLICIT_MULTI_DOMAIN_NOT_SUPPORTED。MODEL_WIDE 保持在当前模型内。严格域过滤的 entity-value 接口仍可使用；无域来源证明的 display 接口在显式范围下不调用；model-only metadata 接口在显式范围下拒绝，并保留错误码，不追加指标追问。

这证明了“不扩大授权”的约束，但不等于完成了“model 81 / [205] 可以正常严格查询”的业务能力。故完整合同验收仍 BLOCKED。外部修复建议和后端接入要求详见 `external_service_fix_candidates.md`；所有外部源码哈希见 `source_contract_audit.json`。未修改 Oagnet、sql-translator 或业务后端仓库。

## 验证

| 项目 | 结果 |
| --- | --- |
| Scope 专项 | 88 / 88 PASS |
| Phase 0B 基线 | 1763 passed / 29 failed |
| 最终完整分批回归 | 1853 passed / 27 failed |
| 新增测试 | 88 |
| 缺失基线 nodeid | 0 |
| Old pass → new fail | 0 |
| Collection errors | 0 |
| 显式域/历史/Pending/Dataset 扩权断言 | 0 违规 |
| Cache 跨 Scope 复用断言 | 0 违规 |
| 多域静默转 model-wide | 0 |
| Prompt / Regex 修改 | 0 / 0 |
| Production V2 routing changed | NO |
| 真实模型调用 / 生产数据写操作 | 0 / 0 |
| Cross-service patches | NO |

两项既有 OpenAPI 标头合同失败现在通过；27 项剩余失败均为 Phase 0B 已有失败。测试输入与少量合同断言迁移有单独记录，没有为了减少失败数量修改业务期望。

最终权威证据为 `bounded_suite_final/aggregate.json`。本机内存不足造成单进程 pytest MemoryError/中断，Git archive 也出现 inflate out of memory；中断运行不计作 Gate。最终使用相同离线网络阻断与固定日期夹具，按每批六个测试模块串行执行，1880 个 nodeid 全部有结果。这里声明分批回归结果，不声明已验证单进程执行顺序的等价性。

功能提交的 585 个 tracked 文件已核对开发目录与 Git 工作区 SHA-256，并以工作区原始字节重算 Git blob ID，对照提交树：全部 MATCH，unexpected tracked drift = 0。关闭证据提交后再次进行同一全文件校验；结果在终端收尾中报告。所有同步均有明确文件清单，排除真实 .env、日志、缓存、备份和凭据。

## 后续边界

最高阻塞是 Oagnet 显式域查询的额外 `-1` 范围。业务后端的服务令牌及状态标头仍需部署接入验证。只有外部服务修复并经严格集合验收后，才能取消相应 fail-closed guard 并重新评估完整 Scope Contract。回滚方法见 `rollback_manifest.json`；未执行回滚或合并。
