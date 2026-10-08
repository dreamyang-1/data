# DreamFly Structured Output Capability

**STRICT_SCHEMA_NOT_SUPPORTED**

2026-09-12 对 `https://api.dreamfly.art/v1/responses` 执行了两次 PRIVATE capability请求，使用 qwen3.7-max、temperature=0、retry=0。请求格式采用 Responses API的 `text.format={type: json_schema, name, schema, strict: true}`；没有通过完整 V2业务链调用。

| Probe | 对抗约束 | HTTP | Provider结果 |
|---|---|---:|---|
| max-items-zero | Schema要求数组 `maxItems=0`，提示词要求输出一项 | 400 | `unsupported_response_format`，参数 `text.format` |
| enum-a-only | Schema只允许 `A`，提示词要求 `B` | 400 | `unsupported_response_format`，参数 `text.format` |

两次响应都明确说明当前不支持 `json_schema` response format，并建议使用 `json_object` 或提示词约束。这是能力拒绝，不是“返回了JSON”或模型偶然遵守。前两次结论没有歧义，因此第三次调用未执行。

PRIVATE证据：

- `max-items-zero.json` SHA-256 `f4210dcc514009f4ba00f5d5e119ce89e416d6b1af5ec9f3a6e4e49881d29285`
- `enum-a-only.json` SHA-256 `18bb17b86af2d54f23d4c4e9fb1689aa65312402827ab5b394563e7ad561860d`
- `manifest.json` SHA-256 `18f9e9ac6e6166075a7e8555cb01a5780325399fc779c68103e1df9502aff272`

原始 request payload、允许公开的响应 headers和raw response只保存在 `.eval_private/round2-2-schema-capability/`，不进入 Git。凭据未写入报告。

当前 `RecognitionModelClient` 的正式配置仍使用 Chat Completions `json_object`，并在 system message中携带动态 Schema。本轮不切换 Provider协议、不继续试探字段、不改 Prompt。本地 exact Schema校验是必需的最终执行边界。
