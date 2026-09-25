"""Build isolated Oagnet Milvus indexes from governed semantic metadata."""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# A migration command is always explicit about its target backend.
os.environ["VECTOR_STORE_BACKEND"] = "milvus"

from embedding import embed_documents  # noqa: E402
from mysql_tool import load_complete_entity_attribute_vector_source  # noqa: E402
from vector_store import (  # noqa: E402
    MilvusVectorStore,
    rebuild_index_by_scope,
    rebuild_index_for_tables_by_scope,
    replace_entity_attribute_index,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--semantic-model-id", type=int, required=True)
    parser.add_argument("--business-domain-id", type=int, required=True)
    parser.add_argument("--data-source-id", type=int)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    rows = load_complete_entity_attribute_vector_source(
        args.semantic_model_id, args.business_domain_id
    )
    per_attribute = Counter(
        (str(row["entity_name"]), str(row["attr_name"])) for row in rows
    )
    preview = {
        "semantic_model_id": args.semantic_model_id,
        "business_domain_id": args.business_domain_id,
        "entity_value_count": len(rows),
        "per_attribute": [
            {"entity": key[0], "attribute": key[1], "count": count}
            for key, count in sorted(per_attribute.items())
        ],
    }
    if args.dry_run:
        print(json.dumps({"dry_run": True, **preview}, ensure_ascii=False, indent=2))
        return 0

    store = MilvusVectorStore()
    semantic = rebuild_index_by_scope(
        store,
        embed_documents,
        args.semantic_model_id,
        args.business_domain_id,
    )
    physical = None
    if args.data_source_id is not None:
        physical = rebuild_index_for_tables_by_scope(
            store,
            embed_documents,
            args.data_source_id,
            args.semantic_model_id,
        )
    values = replace_entity_attribute_index(
        store,
        embed_documents,
        semantic_model_id=args.semantic_model_id,
        business_domain_id=args.business_domain_id,
    )
    print(json.dumps({
        "dry_run": False,
        "health": store.health_check(),
        "semantic": semantic,
        "physical": physical,
        "entity_values": values,
        "source_preview": preview,
    }, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
