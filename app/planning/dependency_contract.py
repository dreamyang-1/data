"""Small deterministic contracts for dependent query routing and delivery.

An upstream answer is prose, not a dataset schema.  Prefer the planner's typed
projection, and keep local result operations only when that projection and its
scope are actually present in the predecessor contract.
"""
from __future__ import annotations

import re
import copy
from typing import Any


def key_kind(value: str) -> str:
    value = re.sub(r"[\s（）()\[\]【】]", "", value.lower()).rsplit(".", 1)[-1]
    if re.search(r"(?:^|_)id$|编号$|标识$", value):
        return "id"
    if re.search(r"(?:^|_)code$|编码$|代码$", value):
        return "code"
    return "display"


def projection(extraction: dict[str, Any] | None) -> list[tuple[str, str]]:
    result = []
    for key in ("指标", "维度", "展示字段"):
        for item in (extraction or {}).get(key) or []:
            if isinstance(item, str):
                result.append(("", item))
            elif isinstance(item, dict):
                name = str(item.get("field") or item.get("name") or "").strip()
                if name:
                    result.append((str(item.get("entity") or ""), name))
    return result


def structured_query_changed(target: dict | None, sources: list[dict | None]) -> bool:
    """Unknown source coverage is not permission to reuse an unrelated table."""
    wanted = projection(target)
    if not wanted:
        return False  # legacy unstructured local-operation path
    for source in sources:
        available = projection(source)
        if not source or not all(
            any(name == old_name and owner == old_owner
                for old_owner, old_name in available)
            for owner, name in wanted
        ):
            continue
        if any(target.get(key) != source.get(key) for key in ("过滤条件", "时间粒度")
               if target.get(key)):
            continue
        # Aggregation grain changes require SQL, not projection of old aggregates.
        if target.get("指标") and target.get("维度") != source.get("维度"):
            continue
        return False
    return True


def dependency_selection_complete(reference, loaded_row_count, source_extraction):
    """A declared Top N is the complete selected set, never the whole population."""
    from minio_followup_store import dataset_source_complete
    if dataset_source_complete(reference,loaded_row_count):
        return True
    spec=(source_extraction or {}).get('结果操作') or {}
    planned=spec.get('operation')
    log=list(reference.transformation_log)
    if not isinstance(planned,dict) or not log or not reference.parent_dataset_ids:
        return False
    actual=copy.deepcopy(log[-1])
    actual.pop('input_binding',None)
    if actual!=planned:
        return False
    steps=planned.get('operations',[]) if planned.get('type')=='pipeline' else [planned]
    if not any(step.get('type') in {'limit','sort_limit','extrema'} for step in steps):
        return False
    # Prove source population coverage independently of this exact, recorded
    # final selection. Earlier truncations/limits remain disqualifying.
    proof=reference.to_dict()
    proof['transformation_log']=log[:-1]
    return dataset_source_complete(proof,loaded_row_count)


def reuse_dependency_rankings(plan):
    """Rank an identical complete predecessor query locally, without re-querying its population."""
    from app.domain.models import PrimaryIntent
    from app.planning.task_dag import database_query_choice
    indexed={task.task_id:task for task in plan.tasks}
    tasks=[]
    for task in plan.tasks:
        target=task.extraction or {}
        source=indexed.get(task.depends_on[0]) if len(task.depends_on)==1 else None
        prior=(source.extraction or {}) if source else {}
        exact=bool(prior and projection(target) and all((target.get(key) or [])==(prior.get(key) or [])
            for key in ('实体','指标','维度','展示字段','过滤条件')) and target.get('时间粒度')==prior.get('时间粒度'))
        derived=bool(re.search(r'增长|环比|同比|平均|占比|差额',str(target.get('输出要求') or '')))
        if (not exact or derived or prior.get('限制') is not None or database_query_choice(target) is False
                or target.get('结果操作') or task.primary_intent not in {PrimaryIntent.METRIC_QUERY,PrimaryIntent.DETAIL_QUERY}):
            tasks.append(task);continue
        steps=[]
        available={name for _,name in projection(prior)}
        invalid_sort=len(target.get('排序') or [])>1
        for item in target.get('排序') or []:
            if not isinstance(item,dict) or item.get('field') not in available or item.get('order') not in {'asc','desc'}:
                invalid_sort=True;break
            steps.append({'type':'sort','field':item['field'],'descending':item['order']=='desc'})
        if invalid_sort:
            tasks.append(task);continue
        limit=target.get('限制')
        if type(limit) is int and 1<=limit<=10_000:
            steps.append({'type':'limit','count':limit})
        if not steps:
            tasks.append(task);continue
        extraction=copy.deepcopy(target)
        extraction.update({'意图':'指标计算','是否查询数据库':'否',
            '结果操作':{'source_task_id':source.task_id,
                'operation':steps[0] if len(steps)==1 else {'type':'pipeline','operations':steps}}})
        tasks.append(task.model_copy(update={'question':'根据前序任务的完整结果'+task.question+'，不再查询数据库',
            'extraction':extraction,'primary_intent':PrimaryIntent.COMPARISON_ANALYSIS}))
    return plan.model_copy(update={'tasks':tasks})


def sql_column_bindings(sql: str, columns: list[str]) -> dict[str, str]:
    """Capture only literal qualified projections, never infer join identities.

    This is provenance from already validated/executed SQL, not a SQL parser or
    security gate. Complex expressions are deliberately left unmapped.
    """
    pairs = re.findall(
        r"(?<![\w.])(`?[A-Za-z_]\w*`?\s*\.\s*`?[A-Za-z_]\w*`?)"
        r"\s+AS\s+(?:`([^`]+)`|\"([^\"]+)\"|\[([^\]]+)\]|([A-Za-z_]\w*))",
        sql, re.I,
    )
    result: dict[str, str] = {}
    conflicts: set[str] = set()
    for field, quoted, double, bracket, bare in pairs:
        alias = quoted or double or bracket or bare
        if alias not in columns:
            continue
        field = re.sub(r"[\s`]", "", field)
        if alias in result and result[alias] != field:
            conflicts.add(alias)
        result[alias] = field
    return {alias: field for alias, field in result.items() if alias not in conflicts}


def missing_display_fields(extraction: dict | None, columns: list[str]) -> list[str]:
    # Exact requested field/alias matching, with common *field* aliases (not
    # entity guessing). The live ASL service remains the authority for binding.
    aliases = (
        ("商品名称", "产品名称", "货品名称", "product_name", "goods_name"),
        ("销售员姓名", "销售人员姓名", "业务员姓名", "业务员名称", "salesperson_name"),
        ("医院名称", "hospital_name"),
        ("经销商名称", "dealer_name"),
    )
    def normalize(value):
        return re.sub(r"[\s`（）()]", "", value).lower().rsplit(".", 1)[-1]
    available = {normalize(str(column)) for column in columns}
    missing = []
    for item in (extraction or {}).get("展示字段") or []:
        name = str(item.get("field") or item.get("name") or "") if isinstance(item, dict) else str(item)
        accepted = {normalize(name)}
        for group in aliases:
            if normalize(name) in group:
                accepted.update(group)
        if not accepted & available:
            missing.append(name)
    return missing
