# Real-Business Failure Closure Round 2.2

**SCHEMA_BOUNDARY_FIXED_BUT_BUSINESS_FAILURE_UNCHANGED**

## 结果

| Gate | 状态 |
|---|---|
| Exact issued Dynamic Schema enforcement | **PASS** |
| 违规输出不进入 Runtime | **PASS** |
| DreamFly strict structured output | **STRICT_SCHEMA_NOT_SUPPORTED** |
| RB50-10 / 37 / 25定向复验 | **COMPLETE** |
| Business correctness improvement | **NOT_PROVEN** |
| Core50 / Benchmark rerun | **NOT_RUN_BY_DIRECTIVE** |

Phase A已经把安全责任放在客户端边界：raw JSON先按本次真正下发的动态 Schema校验，随后继续现有Pydantic校验。Round 2.1三条违规raw response离线复验均提前拒绝，且不触发模型调用。

Phase B对 DreamFly Responses strict Schema执行两次对抗Probe。两次都以 `unsupported_response_format` 明确拒绝 `text.format=json_schema`；第三次没有必要。正式结论为 `STRICT_SCHEMA_NOT_SUPPORTED`。当前继续使用 system message动态 Schema加本地强制校验。

Phase C使用新模型响应定向复验三条Case。RB50-10仍违反Schema并提前拒绝；RB50-25结构合规但仍在payload coverage拒绝；RB50-37结构合规并发布Task，却丢失具体产品Filter，属于静默扩大查询范围。安全错误归因更早、更准确，但没有形成可认证的业务正确改善。

## 变更和验证

生产改动只涉及 `RecognitionModelClient.complete`及其局部校验helper；增加明确运行依赖 `jsonschema`。Prompt、模型Schema定义、Role、Source Value语义、Runtime guard、Scope、Query Shape、Core50、scorer、expected result、V1 baseline、Oagnet、SQL Translator和8088均未修改。

最终受影响离线集合：**346 passed / 0 failed / 0 collection errors**。其中Round 2.2专项12项；其余覆盖Recognition、Round1 Source Value、Filter/Target alignment、Context proposal、Context critical slice和A–E完成问题/API桥接。测试模型调用=0、SQL=0。

真实调用预算：

- Provider capability请求2次，均在协议入口被HTTP 400拒绝；retry=0，第三次未用
- Targeted Case模型调用6次，HTTP 200=6；retry=0
- Core50、V1、完整Benchmark、SQL、8088调用均为0

## 停止点

没有观察到任何 `SCHEMA_COMPLIANT_AND_SEMANTICALLY_CORRECT` 的定向Case。RB50-37证明“Schema合规”仍可能静默遗漏业务条件；正确Draft路径需要重新审查CurrentTurn对实体类型与实体值的表达，以及生成阶段如何消费该表达。这已经触及Role/Source Value业务语义，按本轮停止条件不得继续修改。

因此本轮不提出或执行 Prompt修补、Role规则、Source Value repair或第三模型阶段。RB50-32、RB50-36、RB50-27保持原独立根因，未顺手修改。V1与当前8088路由保持不变。
