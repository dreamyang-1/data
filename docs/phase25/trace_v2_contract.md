# Trace V2 契约

Trace V2 覆盖从 REQUEST_RECEIVED 到 RESPONSE_FINALIZED 的 22 个阶段以及 ERROR，并绑定 conversation/message/topic/task/version 与 model/prompt/catalog/policy 版本。只记录摘要、digest、候选/拒绝汇总和经过治理的 payload。禁止 API key、token、密码、Cookie、完整认证 URL、原始全量结果行和未经治理敏感字段。原始问题持久化可关闭，启用时必须先 Sanitizer。当前仅生成 Schema，不替换现有事件系统。
