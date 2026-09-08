from __future__ import annotations

import asyncio
import logging
import re
import time
from decimal import Decimal, InvalidOperation
from contextlib import suppress
from typing import Any, Mapping, Sequence

from minio_followup_store import (
    DatasetReference,
    DatasetScope,
    HybridMinioFollowupStore,
    MinioFollowupStore,
    dataset_reference_from_dict,
)

from app.stores import SessionStore


logger = logging.getLogger(__name__)


_DATASET_REFERENCE_MARKERS = (
    "刚才", "上次", "上一个", "上一份", "上面", "上述", "前面",
    "这个结果", "这些结果", "这份数据", "该结果", "该数据",
)


def is_dataset_operation_followup(question: str) -> bool:
    """Recognize commands that operate on an existing result, independent of intent labels."""
    compact = re.sub(r"\s+", "", question).lower()
    if any(marker in compact for marker in _DATASET_REFERENCE_MARKERS):
        return True
    # Bare operation commands are common after a result.  Fresh-query wording and
    # explicit dates keep them on the semantic-query path instead of reusing history.
    if any(marker in compact for marker in ("查询", "统计", "分析", "计算", "今年", "去年", "本月", "上月")):
        return False
    if re.search(r"(?:20\d{2}年|\d{1,2}月\d{0,2}日?|最近\d+)", compact):
        return False
    if re.search(
        r"(?:前|top)(?:\d{1,5}|[一二三四五六七八九十]{1,3})(?:名|条|个)?",
        compact,
        re.I,
    ):
        return True
    return any(
        marker in compact
        for marker in (
            "最高的是", "最低的是", "最大的是", "最小的是", "最高和最低",
            "最高、最低", "哪个月最高", "哪个月最低", "最高月", "最低月",
            "排第一", "差多少", "高多少", "多多少", "相差", "差额", "排序", "升序", "降序",
            "从高到低", "从低到高", "从大到小", "从小到大", "只看", "只保留",
            "筛选", "过滤", "导出", "下载", "展示前", "显示前", "返回前",
        )
    )


def plan_dataset_followup(
    question: str,
    columns: Sequence[str],
    rows: Sequence[Mapping[str, Any]],
    *,
    ordering_proof: Mapping[str, Any] | None = None,
    source_complete: bool = True,
) -> dict[str, Any] | None:
    """Map only unambiguous local follow-ups to whitelisted dataset operations.

    Returning ``None`` deliberately routes the request back through ASL/SQL.  This
    fail-closed behaviour prevents a vague follow-up from being calculated against
    the wrong historical snapshot.
    """
    compact = re.sub(r"\s+", "", question)
    # Explicit display limits preserve row order even for numeric datasets.
    display = re.fullmatch(r"(?:只看|只显示|只展示|只返回|只保留|显示|展示|返回)前(\d{1,5}|[一二三四五六七八九十]{1,3})条[。？！?!]?", compact)
    if display:
        return {'type': 'limit', 'count': _ranking_count(display.group(1))}
    if not source_complete:
        candidate = plan_dataset_followup(question, columns, rows, ordering_proof=ordering_proof)
        family = dataset_operation_family(candidate)
        if family in {'DISPLAY_LIMIT', 'PROJECTION'}:
            return candidate
        if family == 'LOCAL_SORT' and any(marker in compact for marker in _DATASET_REFERENCE_MARKERS):
            return candidate
        return None
    # “其他筛选条件不变” describes inherited query scope; it is not itself a
    # request to filter the materialized result. Remove this suffix before
    # choosing the local operation so “只返回前5个” reaches the limit parser.
    compact = re.sub(
        r"，?(?:其他|其余)(?:筛选)?条件不变[。！!？?]?$",
        "",
        compact,
    )
    # A result reference followed by a new relationship, metric or comparison
    # is not a presentation operation over the old rows.  Examples include
    # “第一名经销商合作哪些厂家”, “这些科室对应的销售额” and
    # “对比前两名经销商的销售差异”.  Route these turns back through canonical
    # rewrite + ASL/SQL so new projections and joins cannot be skipped.
    if re.search(
        r"(?:合作|集中|分布|对应).{0,30}(?:厂家|医院|科室|销售额|金额|订单)"
        r"|(?:对比|比较).{0,30}(?:差异|差额|销售额|金额|订单)",
        compact,
    ):
        return None
    sheet_filter: dict[str, Any] | None = None
    if "_sheet_name" in columns:
        sheet_names = {
            str(row["_sheet_name"])
            for row in rows
            if row.get("_sheet_name") is not None
        }
        mentioned_sheets = _explicit_sheet_mentions(question, sheet_names)
        if len(mentioned_sheets) == 1:
            sheet_filter = {
                "type": "filter",
                "field": "_sheet_name",
                "operator": "eq",
                "value": mentioned_sheets[0],
            }
    numeric_columns = [
        column
        for column in columns
        if not str(column).startswith("_")
        if any(
            _is_numeric(row.get(column))
            for row in rows
        )
    ]

    if "客单价" in compact:
        sales_column = _single_matching_column(columns, ("销售额", "成交额", "实付金额"))
        order_column = _single_matching_column(columns, ("订单量", "订单数"))
        if sales_column is None or order_column is None:
            return None
        operations: list[dict[str, Any]] = [{
            "type": "derive",
            "left_field": sales_column,
            "right_field": order_column,
            "operator": "divide",
            "result_field": "客单价",
        }]
        if any(word in compact for word in ("谁高", "最高", "最大")):
            operations.extend([
                {"type": "sort", "field": "客单价", "descending": True},
                {"type": "limit", "count": 1},
            ])
        elif any(word in compact for word in ("谁低", "最低", "最小")):
            operations.extend([
                {"type": "sort", "field": "客单价", "descending": False},
                {"type": "limit", "count": 1},
            ])
        operation: dict[str, Any] = (
            operations[0]
            if len(operations) == 1
            else {"type": "pipeline", "operations": operations}
        )
        return _with_sheet_filter(sheet_filter, operation)

    wants_maximum = any(word in compact for word in ("最高", "最大"))
    wants_minimum = any(word in compact for word in ("最低", "最小"))
    extrema_target = _mentioned_or_unique_column(compact, numeric_columns)
    wants_difference = any(word in compact for word in ("差多少", "高多少", "相差", "差额"))
    if wants_difference and len(rows) == 2 and extrema_target is not None:
        return _with_sheet_filter(sheet_filter, {
            "type": "extrema_difference",
            "field": extrema_target,
            "result_field": f"{extrema_target}差额",
        })
    if wants_maximum and wants_minimum and extrema_target is not None:
        operation = {
            "type": "extrema",
            "field": extrema_target,
        }
        if wants_difference:
            operation.update(
                include_difference=True,
                result_field=f"{extrema_target}差额",
            )
        return _with_sheet_filter(sheet_filter, operation)

    if wants_difference and len(rows) >= 2 and extrema_target is not None:
        return _with_sheet_filter(sheet_filter, {
            "type": "extrema_difference",
            "field": extrema_target,
            "result_field": f"{extrema_target}差额",
        })

    if (wants_maximum or wants_minimum) and extrema_target is not None:
        explicit_count = re.search(r"(?:最高|最低|最大|最小)(\d{1,5}|[一二三四五六七八九十]{1,3})(?:名|条|个)", compact)
        return _with_sheet_filter(sheet_filter, {
            "type": "sort_limit",
            "field": extrema_target,
            "descending": wants_maximum,
            "count": _ranking_count(explicit_count.group(1)) if explicit_count else 1,
        })

    if any(marker in compact for marker in (
        "只看", "只保留", "只显示", "仅显示", "只要", "保留字段", "保留列",
    )):
        requested_business_columns = [
            name for name in (
                "医院名称", "医院等级", "经销商名称", "供应商名称",
                "商品名称", "产品名称", "交易日期", "订单号",
            )
            if name in compact
        ]
        if any(name not in columns for name in requested_business_columns):
            return None
        selected = [column for column in columns if column and column in compact]
        if selected:
            return _with_sheet_filter(
                sheet_filter, {"type": "select", "columns": selected}
            )

    if any(marker in compact for marker in ("只看", "只保留", "筛选", "过滤")):
        candidates: list[tuple[str, Any]] = []
        for column in columns:
            if sheet_filter is not None and column == "_sheet_name":
                continue
            distinct = {str(row[column]): row[column] for row in rows if row.get(column) is not None}
            for text, value in distinct.items():
                if text and text in compact:
                    candidates.append((column, value))
        if len(candidates) == 1:
            column, value = candidates[0]
            operation = {"type": "filter", "field": column, "operator": "eq", "value": value}
            return _with_sheet_filter(sheet_filter, operation)
        return sheet_filter if not candidates else None

    preview = re.search(r"(?:展示|显示|返回|查看)前?(\d{1,5})条(?:数据|结果)?", compact)
    if preview:
        count = int(preview.group(1))
        if not 1 <= count <= 10_000:
            return None
        return _with_sheet_filter(sheet_filter, {
            "type": "limit",
            "count": count,
        })

    count_pattern = r"(\d{1,5}|[一二三四五六七八九十]{1,3})"
    rank = re.search(rf"(?:前|Top){count_pattern}(?:名|条|个)?", compact, re.I)
    bottom = re.search(rf"(?:后|最低){count_pattern}(?:名|条|个)?", compact)
    if rank or bottom:
        match = rank or bottom
        assert match is not None
        target = _mentioned_or_unique_column(compact, numeric_columns)
        if target is None:
            # A closed-form limit replacement over an already ordered result
            # preserves the verified source order. This covers ranked tables
            # with several numeric columns as well as multi-column relation
            # lists. Re-sorting by an arbitrary numeric column would change the
            # user's prior ranking.
            limit_only = bool(re.fullmatch(
                rf"(?:改成|改为|换成|只(?:显示|展示|返回|保留)?|展示|显示|返回)?"
                rf"(?:前|Top){count_pattern}(?:名|条|个)?"
                r"(?:，?(?:其他|其余)(?:筛选)?条件不变)?[。！!？?]?",
                compact,
                re.I,
            ))
            asks_for_ranked_names = bool(re.search(
                rf"(?:前|Top){count_pattern}名", compact, re.I
            ))
            if rank and limit_only and (
                not asks_for_ranked_names or ordering_proof is not None
            ):
                return _with_sheet_filter(sheet_filter, {
                    "type": "limit",
                    "count": _ranking_count(match.group(1)),
                })
            # A one-column name list has no ranking metric.  Natural turns
            # such as “前五个” mean preview the first five existing rows, not
            # start a new metric query and ask which metric to rank by.
            visible_columns = [
                column for column in columns if not str(column).startswith("_")
            ]
            if (
                len(visible_columns) == 1
                and rank
                and not asks_for_ranked_names
            ):
                return _with_sheet_filter(sheet_filter, {
                    "type": "limit",
                    "count": _ranking_count(match.group(1)),
                })
            return None
        operation = {
            "type": "sort_limit",
            "field": target,
            "descending": bool(rank),
            "count": _ranking_count(match.group(1)),
        }
        return _with_sheet_filter(sheet_filter, operation)

    aggregation_words = {
        "平均": "avg", "均值": "avg", "合计": "sum", "总和": "sum",
        "最大": "max", "最高": "max", "最小": "min", "最低": "min",
    }
    function = next((value for word, value in aggregation_words.items() if word in compact), None)
    if function:
        target = _mentioned_or_unique_column(compact, numeric_columns)
        if target is None:
            return None
        group_by = [column for column in columns if f"按{column}" in compact]
        if len(group_by) > 1:
            return None
        operation = {
            "type": "aggregate",
            "group_by": group_by,
            "aggregations": [
                {"field": target, "function": function, "alias": f"{function}_{target}"}
            ],
        }
        return _with_sheet_filter(sheet_filter, operation)

    if any(word in compact for word in ("排序", "升序", "降序", "从高到低", "从低到高", "从大到小", "从小到大")):
        # A business ranking requires a numeric measure.  Falling back to the
        # only visible text column turns requests such as “按整体业务规模排序”
        # into an alphabetical dealer-name sort and silently changes meaning.
        # If the requested measure is absent from the materialized result,
        # return None so the orchestrator performs a fresh semantic query.
        target = _mentioned_or_unique_column(compact, numeric_columns)
        if target is None:
            return None
        operation = {
            "type": "sort",
            "field": target,
            "descending": any(word in compact for word in ("降序", "从高到低", "从大到小")),
        }
        return _with_sheet_filter(sheet_filter, operation)

    if any(marker in compact for marker in ("只显示", "仅显示", "只要", "保留字段", "保留列")):
        requested_business_columns = [
            name for name in (
                "医院名称", "医院等级", "经销商名称", "供应商名称",
                "商品名称", "产品名称", "交易日期", "订单号",
            )
            if name in compact
        ]
        if any(name not in columns for name in requested_business_columns):
            return None
        selected = [column for column in columns if column and column in compact]
        if selected:
            return _with_sheet_filter(sheet_filter, {"type": "select", "columns": selected})
    return sheet_filter


def dataset_operation_family(operation: Mapping[str, Any] | None) -> str | None:
    if operation is None:
        return None
    return {'limit':'DISPLAY_LIMIT', 'sort':'LOCAL_SORT', 'select':'PROJECTION',
            'sort_limit':'GLOBAL_RANKING', 'aggregate':'GLOBAL_RANKING',
            'extrema':'GLOBAL_RANKING', 'extrema_difference':'GLOBAL_RANKING',
            'drilldown':'DRILLDOWN'}.get(str(operation.get('type')))


def presentation_ancestors(
    selected: Mapping[str, Any], references: Sequence[Mapping[str, Any]], *, allow_rank_slice: bool = False,
) -> list[Mapping[str, Any]]:
    """Walk only proven slice edges; filters, joins and changed snapshots are barriers.

    Undoing a sort-limit is safe only when a new global operation will be planned
    on the complete parent. Display expansion must preserve the existing order.
    """
    by_id = {item.get('dataset_id'): item for item in references}
    result: list[Mapping[str, Any]] = []
    visited = {selected.get('dataset_id')}
    current = selected
    while True:
        parents = current.get('parent_dataset_ids') or []
        if len(parents) != 1 or parents[0] in visited:
            return result
        parent = by_id.get(parents[0])
        if parent is None or current.get('source_ref') != parents[0]:
            return result
        if any(current.get(key) != parent.get(key) for key in (
            'scope', 'columns', 'snapshot_id', 'data_as_of', 'semantic_model_id', 'business_domain_ids',
        )):
            return result
        current_log, parent_log = current.get('transformation_log') or [], parent.get('transformation_log') or []
        if len(current_log) != len(parent_log) + 1 or current_log[:-1] != parent_log:
            return result
        operation = current_log[-1]
        if not isinstance(operation, Mapping) or operation.get('type') not in (
            {'limit', 'sort_limit'} if allow_rank_slice else {'limit'}
        ):
            return result
        child_rows, parent_rows = current.get('row_count'), parent.get('row_count')
        if type(child_rows) is not int or type(parent_rows) is not int or not 0 <= child_rows <= parent_rows:
            return result
        count = operation.get('count')
        if type(count) is not int or count < 1 or child_rows != min(count, parent_rows):
            return result
        visited.add(parents[0])
        result.append(parent)
        current = parent


def _ranking_count(value: str) -> int:
    if value.isdigit():
        return int(value)
    digits = {
        "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
        "六": 6, "七": 7, "八": 8, "九": 9,
    }
    if value == "十":
        return 10
    if "十" in value:
        tens, ones = value.split("十", maxsplit=1)
        return digits.get(tens, 1) * 10 + digits.get(ones, 0)
    return digits[value]


def _with_sheet_filter(
    sheet_filter: dict[str, Any] | None,
    operation: dict[str, Any],
) -> dict[str, Any]:
    if sheet_filter is None:
        return operation
    return {"type": "pipeline", "operations": [sheet_filter, operation]}


def _explicit_sheet_mentions(question: str, sheet_names: set[str]) -> list[str]:
    """Select sheets only when the user explicitly says Sheet/工作表/页签.

    A sheet called ``销售`` must not turn the ordinary metric phrase ``销售额``
    into an implicit filter. Longest names are evaluated first so ``销售明细``
    is not confused with a shorter ``销售`` sheet.
    """
    result: list[str] = []
    for name in sorted((value for value in sheet_names if value), key=len, reverse=True):
        escaped = re.escape(name)
        suffix = rf"{escaped}\s*(?:sheet|工作表|页签|表页)"
        prefix = rf"(?:sheet|工作表|页签|表页)\s*[‘’'\"“”]?{escaped}[‘’'\"“”]?"
        if re.search(rf"(?:{suffix}|{prefix})", question, flags=re.IGNORECASE):
            result.append(name)
    return result


def _mentioned_or_unique_column(question: str, columns: Sequence[str]) -> str | None:
    mentioned = [column for column in columns if column and column in question]
    if len(mentioned) == 1:
        return mentioned[0]
    return columns[0] if len(columns) == 1 else None


def _single_matching_column(columns: Sequence[str], aliases: Sequence[str]) -> str | None:
    matches = [
        column for column in columns
        if any(alias == column or alias in column for alias in aliases)
    ]
    return matches[0] if len(matches) == 1 else None


def _is_numeric(value: Any) -> bool:
    if value is None or isinstance(value, bool):
        return False
    try:
        return Decimal(str(value).strip().replace(",", "")).is_finite()
    except (InvalidOperation, ValueError):
        return False


def normalize_planned_operation(operation: dict[str, Any]) -> list[dict[str, Any]]:
    """Expand compound planner operations into primitive audited operations."""
    if operation.get("type") != "sort_limit":
        return [operation]
    return [
        {
            "type": "sort",
            "field": operation["field"],
            "descending": operation["descending"],
        },
        {"type": "limit", "count": operation["count"]},
    ]


class DatasetLifecycleCleaner:
    def __init__(
        self,
        store: MinioFollowupStore | HybridMinioFollowupStore,
        sessions: SessionStore,
        *,
        interval_seconds: int,
        batch_size: int,
        report_client: Any | None = None,
        report_bucket: str | None = None,
    ) -> None:
        self.store = store
        self.sessions = sessions
        self.interval_seconds = interval_seconds
        self.batch_size = batch_size
        self.report_client = report_client
        self.report_bucket = report_bucket
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="dataset-lifecycle-cleaner")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task

    async def cleanup_once(self) -> int:
        references = await self.sessions.list_expired_dataset_references(
            now_epoch=time.time(), limit=self.batch_size
        )
        removed = 0
        for raw in references:
            try:
                reference = dataset_reference_from_dict(raw)
                # Cleaner uses the immutable scope embedded in the trusted Redis record.
                await asyncio.to_thread(
                    self.store.delete_dataset,
                    reference,
                    current_scope=reference.scope,
                )
            except Exception as exc:
                code = getattr(exc, "code", "")
                if code not in {"NoSuchKey", "NoSuchObject"}:
                    logger.warning("failed to clean expired dataset %s: %s", raw.get("dataset_id"), exc)
                    continue
            await self.sessions.delete_dataset_reference(str(raw["dataset_id"]))
            removed += 1
        if self.report_client is not None and self.report_bucket:
            report_references = await self.sessions.list_expired_report_references(
                now_epoch=time.time(), limit=self.batch_size
            )
            for raw in report_references:
                try:
                    await asyncio.to_thread(
                        self.report_client.remove_object,
                        self.report_bucket,
                        str(raw["object_name"]),
                    )
                except Exception as exc:
                    code = getattr(exc, "code", "")
                    if code not in {"NoSuchKey", "NoSuchObject"}:
                        logger.warning(
                            "failed to clean expired report %s: %s",
                            raw.get("report_id"), exc,
                        )
                        continue
                await self.sessions.delete_report_reference(str(raw["report_id"]))
                removed += 1
        return removed

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.cleanup_once()
            except Exception:
                logger.exception("dataset lifecycle cleanup cycle failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_seconds)
            except TimeoutError:
                pass


def scope_for_request(request: Any) -> DatasetScope:
    return DatasetScope(
        tenant_id=request.tenant_id,
        user_id=request.user_id,
        application_id=request.application_id,
        conversation_id=request.conversation_id,
        authorized_semantic_scope_fingerprint=(request.authorized_semantic_scope.fingerprint() if request.authorized_semantic_scope else ''),
    )


def restore_reference(raw: Mapping[str, Any]) -> DatasetReference:
    return dataset_reference_from_dict(raw)
