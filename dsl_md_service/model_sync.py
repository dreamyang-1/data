"""语义模型描述文件自更新。

轮询平台库 semantic_model 表，模型 update_time 比本地描述文件新时，从库里
拼装 YML（结构同平台导出），调抽取模型重新生成 md，覆盖
files/semantic_model_{id}.md。结果先写临时文件并校验章节结构，不通过就
保留旧文件。

用法：
  python model_sync.py              # 处理所有过期的模型
  python model_sync.py --model-id 81 --force   # 强制重抽指定模型
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import time
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pymysql
import yaml

from main import FILES_DIR, _call_model, _fill_business_domain_columns

SYNC_LOG = Path(__file__).resolve().parent / "sync.log"

LEVEL_LABEL = {1: "原子指标", 2: "派生指标", 3: "复合口径指标"}


def log(msg: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    with SYNC_LOG.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _conn() -> pymysql.connections.Connection:
    return pymysql.connect(
        host=os.environ["DATA_AGENT_MYSQL_HOST"],
        port=int(os.getenv("DATA_AGENT_MYSQL_PORT", "3306")),
        user=os.environ["DATA_AGENT_MYSQL_USER"],
        password=os.environ["DATA_AGENT_MYSQL_PASSWORD"],
        database=os.getenv("DATA_AGENT_MYSQL_DATABASE", "info_sec_kb_v3"),
        connect_timeout=5,
        read_timeout=15,
        charset="utf8mb4",
    )


def _fetchall(cur, sql: str, args) -> list[dict]:
    cur.execute(sql, args)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _json_or_text(value):
    """JSON 列有些存的是字符串，有些已解析成对象，这里统一成对象；其他内容原样返回。"""
    if not isinstance(value, str):
        return value
    text = value.strip()
    if text[:1] in "[{":
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return value
    return value


def _plain(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def build_model_yml(cur, model_id: int) -> dict:
    """按平台导出 YML 的结构从库里拼装一个模型的语义定义。"""
    domains = _fetchall(
        cur,
        "SELECT id, name, description, access_permission "
        "FROM semantic_model_business_domain WHERE semantic_model_id=%s AND is_deleted=0",
        (model_id,),
    )
    domain_names = {str(d["id"]): d["name"] for d in domains}

    entities = _fetchall(
        cur,
        "SELECT id, code, name, alias, business_domain_id, semantic_model_id, description, "
        "data_source_id, main_table_name, status, view_status, update_frequency, is_deleted "
        "FROM semantic_model_entity_type WHERE semantic_model_id=%s AND is_deleted=0",
        (model_id,),
    )
    entity_ids = [e["id"] for e in entities]

    attrs_by_entity: dict[str, list] = {eid: [] for eid in entity_ids}
    if entity_ids:
        placeholders = ",".join(["%s"] * len(entity_ids))
        for row in _fetchall(
            cur,
            "SELECT entity_type_id, code, attr_name, mapping_table, mapping_column, data_type, "
            "is_required, description, is_primary_key, view_status, query_output "
            f"FROM semantic_model_attribute_config WHERE entity_type_id IN ({placeholders}) "
            "AND is_deleted=0",
            tuple(entity_ids),
        ):
            attrs_by_entity.setdefault(row.pop("entity_type_id"), []).append({
                "attr_code": row["code"],
                "attr_name": row["attr_name"],
                "mapping_table": row["mapping_table"],
                "mapping_column": row["mapping_column"],
                "data_type": row["data_type"],
                "is_required": row["is_required"],
                "attr_description": row["description"],
                "is_primary_key": row["is_primary_key"],
                "view_status": row["view_status"],
                "query_output": row["query_output"],
            })

    # 平台导出把同一条关系同时挂在源实体和目标实体两侧
    relations_by_entity: dict[str, list] = {eid: [] for eid in entity_ids}
    for row in _fetchall(
        cur,
        "SELECT code, name, source_entity_type_id, target_entity_type_id, type, source_field, "
        "target_field, source_table_column_name, target_table_column_name, description, "
        "business_domain_id, status, is_deleted "
        "FROM semantic_model_relation_config WHERE semantic_model_id=%s AND is_deleted=0",
        (model_id,),
    ):
        relations_by_entity.setdefault(row["source_entity_type_id"], []).append(row)
        if row["target_entity_type_id"] != row["source_entity_type_id"]:
            relations_by_entity.setdefault(row["target_entity_type_id"], []).append(row)

    binds_by_entity: dict[str, list] = {}
    entity_by_indicator: dict[str, list] = {}
    for row in _fetchall(
        cur,
        "SELECT entity_id, indicator_code, indicator_name, indicator_logic "
        "FROM semantic_model_entity_bind_indicator WHERE semantic_model_id=%s AND is_deleted=0",
        (model_id,),
    ):
        binds_by_entity.setdefault(row["entity_id"], []).append({
            "indicator_code": row["indicator_code"],
            "indicator_name": row["indicator_name"],
            "indicator_logic": row["indicator_logic"],
        })
        entity_by_indicator.setdefault(row["indicator_code"], []).append(row["entity_id"])

    entity_section = []
    for e in entities:
        entity_section.append({
            "entity_code": e["code"],
            "entity_name": e["name"],
            "entity_alias": e["alias"],
            "business_domain_id": e["business_domain_id"],
            "semantic_model_id": e["semantic_model_id"],
            "entity_description": e["description"],
            "data_source_id": e["data_source_id"],
            "main_table_name": e["main_table_name"],
            "join_logic": e["main_table_name"],
            "entity_status": e["status"],
            "view_status": e["view_status"],
            "update_frequency": e["update_frequency"],
            "is_deleted": e["is_deleted"],
            "semantic_model_attribute_config": attrs_by_entity.get(e["id"], []),
            "semantic_model_relation_config": relations_by_entity.get(e["id"], []),
            "bind_indicator": binds_by_entity.get(e["id"], []),
            "bind_dimension": [],
        })

    dimensions = _fetchall(
        cur,
        "SELECT id, dim_code, dim_name, synonyms, dim_description, dim_type, special_rules, "
        "enum_list, level_list, date_list, entity_attribute, `indicator`, project_id, "
        "semantic_model_id, create_by, is_deleted "
        "FROM semantic_model_dimension WHERE semantic_model_id=%s AND is_deleted=0",
        (model_id,),
    )
    dim_by_rowid = {d["id"]: d for d in dimensions}
    dimension_section = []
    for d in dimensions:
        dimension_section.append({
            "id": d["id"],
            "dim_code": d["dim_code"],
            "dim_name": d["dim_name"],
            "synonyms": d["synonyms"],
            "dim_description": d["dim_description"],
            "dim_type": d["dim_type"],
            "special_rules": d["special_rules"],
            "enum_list": _json_or_text(d["enum_list"]),
            "level_list": _json_or_text(d["level_list"]),
            "date_list": _json_or_text(d["date_list"]),
            "entity_attribute": _json_or_text(d["entity_attribute"]),
            "indicator": _json_or_text(d["indicator"]),
            "project_id": d["project_id"],
            "semantic_model_id": d["semantic_model_id"],
            "create_by": d["create_by"],
            "is_deleted": d["is_deleted"],
        })

    indicator_section = []
    for ind in _fetchall(
        cur,
        "SELECT indicator_code, indicator_name, indicator_level, business_domain_id, unit, "
        "format_rule, business_desc, synonyms, applicable_scenarios, dependence_atomic_indicator, "
        "calculation_formula, global_filters, indicator_logic, bind_dimension_ids, project_id, "
        "semantic_model_id, create_by, is_deleted "
        "FROM semantic_model_indicator WHERE semantic_model_id=%s AND is_deleted=0",
        (model_id,),
    ):
        bind_dim_ids = _json_or_text(ind["bind_dimension_ids"]) or []
        bind_dimensions = [
            {"dim_code": dim_by_rowid[rid]["dim_code"], "dim_name": dim_by_rowid[rid]["dim_name"]}
            for rid in bind_dim_ids
            if isinstance(rid, int) and rid in dim_by_rowid
        ]
        indicator_section.append({
            "indicator_code": ind["indicator_code"],
            "indicator_name": ind["indicator_name"],
            "indicator_level": LEVEL_LABEL.get(ind["indicator_level"], ind["indicator_level"]),
            "business_domain_id": ind["business_domain_id"],
            "unit": ind["unit"],
            "format_rule": ind["format_rule"],
            "business_desc": ind["business_desc"],
            "synonyms": ind["synonyms"],
            "applicable_scenarios": ind["applicable_scenarios"],
            "dependence_atomic_indicator": ind["dependence_atomic_indicator"] or None,
            "calculation_formula": ind["calculation_formula"],
            "global_filters": ind["global_filters"],
            "indicator_logic": ind["indicator_logic"],
            "bind_dimensions": bind_dimensions,
            "bind_entity": entity_by_indicator.get(ind["indicator_code"], []),
            "project_id": ind["project_id"],
            "semantic_model_id": ind["semantic_model_id"],
            "create_by": ind["create_by"],
            "is_deleted": ind["is_deleted"],
        })

    rule_section = []
    for rule in _fetchall(
        cur,
        "SELECT business_domain_ids, start_status, description "
        "FROM semantic_model_general_rule WHERE semantic_model_id=%s AND is_deleted=0",
        (model_id,),
    ):
        ids = [s for s in re.split(r"[,，]", rule["business_domain_ids"] or "") if s.strip()]
        names = [domain_names[i.strip()] for i in ids if i.strip() in domain_names]
        rule_section.append({
            "business_domain_ids": rule["business_domain_ids"],
            "business_domain_names": ",".join(names),
            "start_status": rule["start_status"],
            "description": rule["description"],
        })

    return {
        "semantic_model_business_domain": domains,
        "semantic_model_entity_type": entity_section,
        "semantic_model_indicator": indicator_section,
        "semantic_model_dimension": dimension_section,
        "semantic_model_general_rule": rule_section,
    }


def dump_yml(data: dict) -> str:
    return yaml.safe_dump(
        json.loads(json.dumps(data, default=_plain, ensure_ascii=False)),
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )


def _validate_md(md: str) -> None:
    """检查抽取结果是否含必需章节，不通过就抛异常，不覆盖旧文件。"""
    if "业务语义规范" not in md or "指标" not in md or "维度" not in md:
        raise RuntimeError("抽取结果缺少基本章节，保留旧文件")


async def refresh_model(cur, model_id: int, name: str, force: bool = False) -> str:
    target = FILES_DIR / f"semantic_model_{model_id}.md"
    yml_text = dump_yml(build_model_yml(cur, model_id))
    md = await _call_model(yml_text)
    md = re.sub(r"^```(?:markdown)?\s*|\s*```$", "", md.strip())
    md = _fill_business_domain_columns(md, yml_text)
    _validate_md(md)
    tmp = target.with_suffix(".md.tmp")
    tmp.write_text(md, encoding="utf-8")
    os.replace(tmp, target)
    return f"model_id={model_id}（{name}）已更新"


def heal_registered_urls(cur) -> list[str]:
    """登记地址指向不存在的文件时，改回按模型 id 固定的地址。

    只处理本服务 /files/ 下的地址，且只在原文件确实缺失、固定文件已生成时
    才改；指向其它存储或文件仍然存在的登记值一律不动。
    """
    cur.execute(
        "SELECT id, semantic_desc_file_url FROM semantic_model WHERE is_deleted=0"
    )
    changed: list[str] = []
    for model_id, url in cur.fetchall():
        if not url or "/files/" not in url:
            continue
        current = url.rsplit("/", 1)[-1]
        canonical = f"semantic_model_{model_id}.md"
        if current == canonical:
            continue
        if (FILES_DIR / current).exists():
            continue
        if not (FILES_DIR / canonical).exists():
            continue
        new_url = url[: len(url) - len(current)] + canonical
        cur.execute(
            "UPDATE semantic_model SET semantic_desc_file_url=%s WHERE id=%s",
            (new_url, model_id),
        )
        changed.append(f"model_id={model_id} 地址由 {current} 改为 {canonical}")
    return changed


async def run(model_id: int | None, force: bool) -> None:
    with _conn() as conn, conn.cursor() as cur:
        try:
            for message in heal_registered_urls(cur):
                log("登记地址修正：" + message)
        except Exception as exc:
            # 修正失败不影响描述文件生成
            log(f"登记地址修正失败：{type(exc).__name__} {exc}")
        if model_id is not None:
            cur.execute(
                "SELECT id, name, update_time FROM semantic_model WHERE id=%s AND is_deleted=0",
                (model_id,),
            )
        else:
            cur.execute("SELECT id, name, update_time FROM semantic_model WHERE is_deleted=0")
        models = [
            {"id": r[0], "name": r[1], "update_time": r[2]}
            for r in cur.fetchall()
        ]
        for m in models:
            target = FILES_DIR / f"semantic_model_{m['id']}.md"
            stale = force or not target.exists()
            if not stale and m["update_time"] is not None:
                stale = m["update_time"].timestamp() > target.stat().st_mtime
            if not stale:
                continue
            try:
                log(await refresh_model(cur, m["id"], m["name"], force))
            except Exception as exc:  # 一个模型失败只记录，接着处理下一个
                log(f"model_id={m['id']}（{m['name']}）更新失败：{type(exc).__name__} {exc}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-id", type=int, default=None)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    # 定时任务和手动执行可能同时起来，用文件锁保证同一时间只跑一个实例
    import fcntl

    lock_fd = open(Path(__file__).resolve().parent / ".sync.lock", "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit(0)
    asyncio.run(run(args.model_id, args.force))
