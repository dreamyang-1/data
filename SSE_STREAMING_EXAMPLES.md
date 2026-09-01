# 数据智能体 SSE 流式协议

接口：`POST /agent_chat/stream`，响应类型：`text/event-stream`。

数据智能体与 `New_Agent/api_fn.py` 使用相同的业务事件标签。每个 SSE 块同时在
`event` 行和 JSON 的 `type` 字段中携带标签。

## 事件顺序

```text
thinking（1 到多次）
  -> tool_result（0 到多次）
  -> message_chunk（0 到多次）
  -> answer（有答案时 1 次）
  -> complete（固定 1 次）
```

流建立后的运行异常仍使用 `error`，它不属于正常业务输出标签。

## 完整示例

```text
event: thinking
data: {"type":"thinking","step":"accepted","content":"request accepted","message_id":"m-001"}

event: thinking
data: {"type":"thinking","step":"intent_recognition","content":"已识别用户意图。","meta":{"stage":"INTENT_RECOGNITION","status":"COMPLETED","message":"已识别用户意图。"}}

event: tool_result
data: {"type":"tool_result","step":"extension","name":"inventory_lookup","content":"{\"stock\":18}","status":"success","status_code":200,"error_type":""}

event: message_chunk
data: {"type":"message_chunk","step":"output","index":0,"content":"最近一周销售额为","role":"assistant","node":"data_analysis","is_last":false}

event: message_chunk
data: {"type":"message_chunk","step":"output","index":1,"content":"46259 元。","role":"assistant","node":"data_analysis","is_last":true}

event: answer
data: {"type":"answer","step":"output","content":"最近一周销售额为46259 元。","status":"COMPLETED"}

event: complete
data: {"type":"complete","request_id":"...","conversation_id":"conversation-001","status":"COMPLETED","intent":"METRIC_QUERY","answer":"最近一周销售额为46259 元。","step":"output","content":""}

```

## 标签说明

| `type` | 用途 | 主要字段 |
|---|---|---|
| `thinking` | 接收确认、执行进度、心跳 | `step`、`content`、可选 `meta` |
| `tool_result` | 可选 HTTP/MCP 工具执行结果 | `name`、`content`、`status`、`status_code`、`error_type` |
| `message_chunk` | 可直接展示的答案分块 | `content`、`role=assistant`、`node`、`index`、`is_last` |
| `answer` | 完整权威答案 | `content`、`status` |
| `complete` | 固定终止事件和完整业务响应 | `content=""` 以及完整 `AgentResponse` 字段 |

## 后端解析规则

1. 按空行切分 SSE 块。
2. 读取 `event:`，或解析 `data:` JSON 后读取 `type`。
3. 只将 `message_chunk.content` 增量推送到前端。
4. `answer.content` 用于校验和兜底，不要在已有分块后重复追加。
5. 收到 `complete` 后结束，并根据其中的业务 `status` 区分
   `COMPLETED`、`NEEDS_CLARIFICATION`、`CANCELLED` 或 `SAFE_FALLBACK`。
6. `tool_result.status=error` 表示扩展工具失败；核心查询结果是否可用仍以最终
   `complete.status` 和 `reliability` 为准。

正常流不再输出旧标签：`accepted`、`progress`、`heartbeat`、`answer_chunk`、
`completed`、`clarification`、`cancelled`。
