"""Deterministic presentation cleanup for name lists, not transaction facts."""

import hashlib
import json

from app.domain.models import DataQueryResult, Dataset, PrimaryIntent


def _column_role(column: str) -> str | None:
    name = column.rsplit(".", 1)[-1].lower()
    if name in {"name", "医院", "科室", "商品", "产品", "经销商", "厂牌", "厂商"} or name.endswith(("_name", "名称", "姓名")):
        return "name"
    if name in {"id", "code"} or name.endswith(("_id", "_code", "编码", "编号")):
        return "identity"
    return None


def clean_name_list(intent: PrimaryIntent, result: DataQueryResult) -> DataQueryResult:
    """Clean full projected rows; a name is never used as an entity identity.

    Only name/identity-only detail projections qualify. Amounts, dates, order
    facts, aggregate results and data-quality analyses retain their original
    rows. Unseen rows in an upstream export cannot be declared cleaned.
    """
    dataset = result.dataset
    if intent != PrimaryIntent.DETAIL_QUERY or result.asl.get("metrics") or not dataset.rows:
        return result
    roles = {column: _column_role(column) for column in dataset.columns}
    names = [column for column, role in roles.items() if role == "name"]
    if not names or None in roles.values():
        return result
    # Only explicit missing-value markers, not business labels such as 未知/其他.
    missing = {"", "-", "--", "—", "–", "null", "none", "n/a"}
    rows, seen = [], set()
    invalid = duplicates = trimmed = 0
    for source in dataset.rows:
        row = dict(source)
        for column in names:
            if isinstance(row.get(column), str):
                row[column] = row[column].strip()
        if all(row.get(column) is None or (
            isinstance(row.get(column), str) and row[column].casefold() in missing
        ) for column in names):
            invalid += 1
            continue
        key = json.dumps([row.get(column) for column in dataset.columns], ensure_ascii=False, default=str)
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        trimmed += row != source
        rows.append(row)
    if not (invalid or duplicates or trimmed):
        return result
    partial = dataset.truncated or (dataset.total_row_count or 0) > dataset.row_count
    note = (
        f"名单清理：本次返回 {dataset.row_count} 行，去除 {duplicates} 行完全重复记录、"
        f"{invalid} 行空名称或占位记录，保留 {len(rows)} 行。"
        "按返回的完整行去重；同名但不同编号的记录不会合并，未按名称推断实体数量。"
    )
    if partial:
        note += "仅清理本次返回的预览，全量去重后数量未知。"
        if result.result_file_url:
            note += "附件是上游原始完整结果，尚未进行本次清理。"
    fingerprint = hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()[:20]
    cleaned = Dataset.model_validate({
        **dataset.model_dump(), "rows": rows, "row_count": len(rows),
        "total_row_count": len(rows), "truncated": partial,
        "snapshot_id": f"{dataset.snapshot_id[:210]}:clean:{fingerprint}",
    })
    transform = {
        "type": "NAME_LIST_CLEANUP", "source_snapshot_id": dataset.snapshot_id,
        "source_row_count": dataset.row_count, "source_total_row_count": dataset.total_row_count,
        "source_truncated": partial, "duplicate_rows_removed": duplicates,
        "invalid_rows_removed": invalid, "trimmed_rows": trimmed,
        "returned_row_count": len(rows), "message": note,
    }
    return result.model_copy(update={
        "dataset": cleaned,
        # Never offer a raw full export as the cleaned, complete list.
        "result_file_url": result.result_file_url if partial else None,
        "execution_transforms": [*result.execution_transforms, transform],
    })


def cleanup_message(result: DataQueryResult) -> str:
    return "\n".join(str(item["message"]) for item in result.execution_transforms
                     if item.get("type") == "NAME_LIST_CLEANUP" and item.get("message"))
