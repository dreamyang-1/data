# `/agent/query` 语义证据契约

## 目标

`/agent/query` 不仅返回通过校验的 ASL，还返回由 Oagnet 根据本次召回快照和 SQL 语义层生成的 `semantic_evidence`。下游 DataAnalysis Agent 只能消费、校验和透传这些事实，不能自行补写指标编码、指标名称、公式或业务域。

请求字段保持兼容：

- `query`：必填，自然语言问题。
- `semantic_model_id`：必填，正整数。
- `business_domain_id`：可选，兼容旧版单业务域调用。
- `business_domain_ids`：可选，显式多业务域列表；留空表示在语义模型内自动检索。
- 同时传单数和复数字段时，两者必须严格等价，否则返回 `422`。

## 新增响应字段

`semantic_evidence` 为新增字段，原有 `result` 仍是 ASL JSON 字符串。

```json
{
  "success": true,
  "result": "{...ASL 2.0 JSON...}",
  "semantic_evidence": {
    "evidence_version": "1.0",
    "producer": "OAGNET",
    "semantic_model_id": 6,
    "requested_business_domain_ids": [7, 10],
    "resolved_business_domain_ids": [7, 10],
    "selected_metrics": [
      {
        "canonical_code": "actual_payment_amount",
        "canonical_name": "实付金额",
        "semantic_model_id": 6,
        "business_domain_id": 7,
        "calculation_formula": "actual_payment_amount=SUM(order_info.pay_amount)",
        "formula_source": "calculation_formula",
        "formula_signature": "sha256:...",
        "global_filters": [],
        "metadata_fingerprint": "sha256:...",
        "metadata_source": "MYSQL_SEMANTIC_LAYER",
        "sql_verified": true,
        "retrieval_record_id": "sm6_bd7:metric:actual_payment_amount",
        "retrieval_score": 0.93
      }
    ],
    "asl_signature": "sha256:...",
    "evidence_fingerprint": "sha256:..."
  }
}
```

字段含义：

- `requested_business_domain_ids`：调用方显式限定的业务域；AUTO 模式为空。
- `resolved_business_domain_ids`：本次 ASL 实际选中指标所属的业务域，不等于简单回显请求值。
- `canonical_code` / `canonical_name`：SQL 语义层中的规范指标编码和名称。
- `calculation_formula`：SQL 语义层中的计算公式；公式为空时才读取 SQL 中的指标逻辑。
- `formula_signature`：规范化公式的 SHA-256 指纹。
- `global_filters`：语义层指标全局过滤条件，是指标口径的一部分。
- `metadata_fingerprint`：规范编码、名称、模型、业务域、公式和全局过滤条件的稳定指纹。
- `asl_signature`：响应中 `result` 原始字符串的 SHA-256 指纹。
- `evidence_fingerprint`：除自身外整个证据对象的 SHA-256 指纹。
- `metadata_source=MYSQL_SEMANTIC_LAYER` 且 `sql_verified=true`：已用当前 SQL 元数据核验。
- `metadata_source=VECTOR_INDEX_FALLBACK` 且 `sql_verified=false`：SQL 临时不可用，使用本次向量召回快照降级；下游应降低置信度或稍后重试。

SHA-256 字段用于一致性校验和定位数据漂移，不是带密钥的数字签名。服务身份和链路防篡改仍应由内网网关、TLS 或服务认证保证。

指纹计算约定：公式先将连续空白折叠为单个空格；对象使用 UTF-8 编码、按键名排序的紧凑 JSON（分隔符为 `,` 和 `:`，中文不转义）后再计算 SHA-256。`evidence_fingerprint` 计算时不包含 `evidence_fingerprint` 自身。

## 失败规则

- ASL 选择的指标必须来自本次向量召回集合。
- SQL 可访问但已找不到该指标时，说明向量快照陈旧，本次请求失败，不允许静默使用旧口径。
- 同一指标编码在多个业务域存在不同定义且无法由召回记录确定唯一业务域时，本次请求失败，不允许猜测。
- 指标缺少名称或计算公式/逻辑时，本次请求失败，不允许生成不可核验的数值口径。
- 澄清型 ASL 没有选中指标时，`selected_metrics` 为空；AUTO 模式不会伪造 `resolved_business_domain_ids`。
