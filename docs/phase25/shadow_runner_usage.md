# Offline Shadow Runner 使用说明

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
python tools/phase25/run_semantic_v2_shadow.py --input docs/phase2/gold_cases_seed.jsonl
python tools/phase25/run_semantic_v2_shadow.py --input fixture.json --catalog-snapshot docs/phase2/semantic_catalog_inventory.json --output shadow.jsonl
```

输入可为单 JSON、JSONL、现有 gold/failure cases 或带本地目录快照。没有确定性 V2 fixture 时输出 `UNAVAILABLE`，不会伪造计划。存在 fixture 时只做 Schema、适配损失和结构等价比较，标记 `MOCK` 或 `PARTIAL`。工具不导入生产编排器，不访问模型、Redis、MySQL、Milvus、MinIO，不执行 SQL。
