# V2 评测标注指南

1. 只标注可由测试断言、目录、失败断言或正式契约证明的字段。
2. Legacy intent 证据不能自动升级为多轴完整金标。
3. Mention 使用 Unicode code point 左闭右开偏移，并验证切片等于 surface。
4. 目录 ID 必须绑定 catalog version；无法证明写 `NEEDS_BUSINESS_REVIEW`。
5. `annotation_status=COMPLETE` 要求所有 V2 轴、槽位操作、payload、澄清和结果契约均有证据；否则为 PARTIAL。
6. 当前 200 条种子全部为 PARTIAL，完整金标为 0；证据不足时不为追求 80 条目标而编造。
