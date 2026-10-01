# 第一阶段：固定 V2 Context → V1 Execution 主线

## 主线边界

- 生产桥接模式固定为 `V2_CONTEXT_V1_EXECUTION`。
- V2 负责当前轮理解、对话关系、上下文状态、语义编辑和完整问题生成。
- V1 继续负责问题改写、意图、任务拆分、Oagnet/ASL、SQL、结果校验、分析和扩展工具。
- 纯 V2 执行能力扩展状态为 `PAUSED`。本阶段不切换执行器、不改变业务判断、不改变用户可见等待文案。

机器可读清单可通过受信后端接口读取：

```text
GET /v1/data-analysis/diagnostics/bridge-profile
```

## 统一能力与计时点

| 层 | 能力 | 计时操作名 | 计时边界 |
|---|---|---|---|
| V2 | 请求级 Catalog | `v2.catalog.load` | 开始读取当前授权目录至目录可用或失败 |
| V2 | 当前轮识别模型 | `v2.model.current_turn` | 发起模型请求至完整结构化结果校验完成；另记首个内容片段 |
| V2 | 语义绑定模型 | `v2.model.semantic_edits` | 发起模型请求至完整结构化结果校验完成；另记首个内容片段 |
| V2 | 上下文解析 | `v2.context_resolution` | 开始解析当前轮至完整问题、追问或失败结果形成 |
| 桥接 | V1 执行总链路 | `bridge.v1_execution` | 将 V2 完整问题交给 V1 至 V1 返回最终响应 |
| V1 | 问题改写 | `v1.question_rewrite` | 调用改写器至规范化问题形成 |
| V1 | 意图识别 | `v1.intent_recognition` | 调用 V1 分类器至规范化意图形成 |
| V1 | 任务拆分 | `v1.task_decomposition` | 调用多任务规划器至单任务或任务 DAG 形成 |
| V1 | 语义绑定 | `v1.semantic_binding` | 将已验证 ASL 指标绑定回当前请求 |
| 上游 | Oagnet ASL | `upstream.oagnet.asl_generation` | 发起 Oagnet 请求至响应体返回 |
| 上游 | SQL 翻译 | `upstream.sql.translation` | 发起翻译请求至响应体返回 |
| 上游 | SQL 执行 | `upstream.sql.execution` | 发起执行请求至结果响应返回 |
| 校验 | 结果可靠性 | `validation.result_reliability` | 开始最终证据门禁至可靠性结果形成 |
| 分析 | 确定性分析 | `analysis.deterministic` | 开始算法计算至分析结果形成 |
| 分析 | 模型表达 | `analysis.synthesis` | 发起分析总结模型至经证据校验的表达形成 |
| 图表 | 图表规格 | `chart.specification` | 从确定性分析结果读取已验证图表规格 |
| 图表 | MCP 绘图 | `mcp.chart_render` | 工具发现、MCP 调用至图片结果返回 |
| 图表 | 内联回退 | `chart.inline_render` | MCP 无可用图片时生成内联 SVG |

## 计时数据读取

桥接模式下，每个 `/agent_chat` 和 `/agent_chat/stream` 响应的最终 `AgentResponse` 都包含：

```json
{
  "performance_trace": {
    "version": "bridge-timing-v1",
    "runtime_mode": "V2_CONTEXT_V1_EXECUTION",
    "total_duration_ms": 0,
    "terminal_status": "COMPLETED",
    "operations": [],
    "progress": [],
    "slow_operations": []
  }
}
```

流式接口位于最终 `complete.performance_trace`。后台日志同时写入单行：

```text
bridge_performance_trace={...}
```

字段含义：

- `started_after_ms`：真实调用相对本轮服务端处理开始的时间。
- `first_result_after_ms`：从该调用开始到收到首个可用内容片段的时间；当前重点用于 V2 流式模型。
- `duration_ms`：从真实调用开始到完整响应和本地结构校验结束的时间。
- `progress.occurred_after_ms`：真实业务里程碑发生的相对时间。心跳等待文案不进入该列表。
- `slow_operations`：本轮超过 3 秒的真实调用，按耗时倒序列出。

## 数据边界

计时轨迹不记录用户问题、Prompt、模型原文、SQL、结果行、图片 URL、凭据、服务地址或绝对时间。属性只接受少量标量，例如模型名、是否流式、是否降级、任务数和图表数。计时失败不能影响业务响应。

## 十几秒空白的判定方法

1. 找到页面出现空白前最后一个真实 `progress` 里程碑的 `occurred_after_ms`。
2. 查看同一时间范围内已开始但未完成的 `operations`。
3. 模型调用优先比较 `first_result_after_ms` 和 `duration_ms`：前者长表示模型首包等待，二者差值大表示模型持续生成或完整结构校验耗时。
4. Oagnet、SQL、MCP 当前使用完整响应接口，`duration_ms` 表示该上游调用总等待。
5. 如果时间段内没有任何已登记调用，则属于尚未覆盖的本地计算或状态 I/O，应补充精确调用点，不能用等待文案代替根因。
