"""语义建模画布推荐服务（实体表/属性/关联检测/关系推荐）。

从 youoagent-api 的 SemanticModelEntityTypeServiceImpl /
SemanticModelRelationConfigServiceImpl 移植，接口契约（路径、参数、
Resp 包装、VO 字段）与 Java 版保持一致，后端可直接调用或整体切换。

- 元数据库只读查询（semantic_model_table / semantic_model_field /
  semantic_model_entity_type / semantic_model_entity_sub_table_mapping /
  semantic_model_relation_config），pymysql 按请求开短连接。
  所有查询过滤软删除行（is_deleted = 0）——注意：Java 版
  （MyBatis-Plus 无逻辑删除配置）不过滤，这是 Python 版的有意修复。
- 大模型增强经 49 网关 /chat/workflow_chat（与 Java ChatModelBaseApi
  .translateWords 同一入口），失败或空答案静默降级为纯规则结果（留 WARNING 日志）。
  覆盖：实体表推荐（重排 + 实体名）、属性推荐（属性名提炼，不含字母数字）、
  关系推荐（关系名 + 语义描述）。
  模型名必须是网关 LLM_MODELS 支持的名称（当前为 qwen3.7-max；
  qwen3-max 不在列表，网关会静默返回空答案）。
- 全部为只读接口，不落库、不改数据。
- detect_relations / recommend_relations 一次请求固定 4 条 SQL
  （entities/relations/tables/fields_batch 各一条），不随实体数增长。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class SemanticRecommendError(Exception):
    """业务校验失败，接口层转为 Resp code=500。"""


class MetadataReader(Protocol):
    """元数据库只读访问协议，行字段与数据库列同名（snake_case）。

    所有实现必须过滤软删除行（is_deleted = 0），避免已删除的
    表/实体/字段/关系进入推荐结果。
    """

    def entities(self, semantic_model_id: int) -> list[dict[str, Any]]: ...

    def tables(
        self, semantic_model_id: int, data_source_id: int | None
    ) -> list[dict[str, Any]]: ...

    def field_counts(self, table_ids: list[int]) -> dict[int, int]: ...

    def fields(self, table_id: int) -> list[dict[str, Any]]: ...

    def fields_batch(self, table_ids: list[int]) -> dict[int, list[dict[str, Any]]]: ...

    def relations(self, semantic_model_id: int) -> list[dict[str, Any]]: ...


class ModelGateway(Protocol):
    """大模型网关协议：返回模型输出文本，失败返回 None。"""

    def complete(self, query: str, prompt: str) -> str | None: ...


# ====================== 元数据库 pymysql 实现 ======================


class PymysqlMetadataReader:
    """按调用开短连接的只读元数据访问（信息库 192.168.1.223/info_sec_kb_v3）。"""

    def __init__(
        self, *, host: str, port: int, user: str | None, password: str | None,
        database: str, connect_timeout: int = 5, read_timeout: int = 10,
    ) -> None:
        self._conn_kwargs = dict(
            host=host,
            port=port,
            user=user,
            password=password,
            database=database,
            charset="utf8mb4",
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
        )

    def _fetch(self, sql: str, params: tuple) -> list[dict[str, Any]]:
        import pymysql

        conn = pymysql.connect(cursorclass=pymysql.cursors.DictCursor, **self._conn_kwargs)
        try:
            with conn.cursor() as cursor:
                cursor.execute(sql, params)
                return list(cursor.fetchall())
        finally:
            conn.close()

    def entities(self, semantic_model_id: int) -> list[dict[str, Any]]:
        return self._fetch(
            "SELECT id, name, main_table_name, data_source_id, business_domain_id "
            "FROM semantic_model_entity_type "
            "WHERE semantic_model_id = %s AND is_deleted = 0",
            (semantic_model_id,),
        )

    def tables(
        self, semantic_model_id: int, data_source_id: int | None
    ) -> list[dict[str, Any]]:
        sql = (
            "SELECT id, name, comment, data_source_id FROM semantic_model_table "
            "WHERE semantic_model_id = %s AND is_deleted = 0"
        )
        params: list[Any] = [semantic_model_id]
        if data_source_id is not None:
            sql += " AND data_source_id = %s"
            params.append(data_source_id)
        return self._fetch(sql, tuple(params))

    def field_counts(self, table_ids: list[int]) -> dict[int, int]:
        if not table_ids:
            return {}
        placeholders = ",".join(["%s"] * len(table_ids))
        rows = self._fetch(
            f"SELECT table_id, COUNT(*) AS cnt FROM semantic_model_field "
            f"WHERE table_id IN ({placeholders}) AND is_deleted = 0 "
            f"GROUP BY table_id",
            tuple(table_ids),
        )
        return {row["table_id"]: int(row["cnt"]) for row in rows}

    def fields(self, table_id: int) -> list[dict[str, Any]]:
        return self._fetch(
            "SELECT id, name, comment, type, enum_values "
            "FROM semantic_model_field "
            "WHERE table_id = %s AND is_deleted = 0",
            (table_id,),
        )

    _FIELDS_BATCH_CHUNK = 500

    def fields_batch(self, table_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
        """按 table_id 批量取字段，替代逐表查询（detect_relations 消 N+1）。"""
        result: dict[int, list[dict[str, Any]]] = {}
        for start in range(0, len(table_ids), self._FIELDS_BATCH_CHUNK):
            chunk = table_ids[start:start + self._FIELDS_BATCH_CHUNK]
            placeholders = ",".join(["%s"] * len(chunk))
            rows = self._fetch(
                "SELECT id, table_id, name, comment, type, enum_values "
                "FROM semantic_model_field "
                f"WHERE table_id IN ({placeholders}) AND is_deleted = 0",
                tuple(chunk),
            )
            for row in rows:
                result.setdefault(row["table_id"], []).append(row)
        return result

    def relations(self, semantic_model_id: int) -> list[dict[str, Any]]:
        return self._fetch(
            "SELECT id, name, category, type, source_entity_type_id, "
            "target_entity_type_id, source_table_column_name, target_table_column_name "
            "FROM semantic_model_relation_config "
            "WHERE semantic_model_id = %s AND is_deleted = 0",
            (semantic_model_id,),
        )


# ====================== 49 模型网关 ======================


class WorkflowChatGateway:
    """与 Java ChatModelBaseApi.translateWords 相同的 /chat/workflow_chat 入口。"""

    def __init__(
        self, *, base_url: str, api_key: str | None, model_name: str,
        timeout_seconds: float = 15.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model_name = model_name
        self._timeout_seconds = timeout_seconds

    def complete(self, query: str, prompt: str) -> str | None:
        import httpx

        payload = {
            "query": query,
            "model_name": self._model_name,
            "prompt": prompt,
            "stream": False,
            "output_format": True,
        }
        headers = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        try:
            response = httpx.post(
                self._base_url + "/chat/workflow_chat",
                json=payload,
                headers=headers,
                timeout=self._timeout_seconds,
            )
            if response.status_code != 200:
                logger.warning(
                    "模型网关返回 HTTP %s，推荐降级为规则结果: %.200s",
                    response.status_code, response.text,
                )
                return None
            text = response.text
            # 返回为 SSE 格式：data: {"answer": "..."}
            marker = text.find("data:")
            if marker >= 0:
                text = text[marker + 5:].strip()
            answer = json.loads(text).get("answer")
            if not isinstance(answer, str) or not answer.strip():
                # 网关对不在支持列表里的模型名会静默返回空答案，
                # 必须留痕，否则 LLM 增强会长期静默失效。
                logger.warning(
                    "模型网关返回空答案（model=%s），请检查模型名是否在网关支持列表",
                    self._model_name,
                )
                return None
            return answer
        except Exception as exc:
            logger.warning("模型网关调用失败，推荐降级为规则结果: %s", exc)
            return None


# ====================== 纯规则逻辑 ======================

_ENTITY_NAME_SUFFIXES = ("信息表", "记录表", "明细表", "数据表", "管理表", "配置表", "表")


def _has_text(value: str | None) -> bool:
    return bool(value and value.strip())


def _derive_entity_name(table_name: str | None, table_comment: str | None) -> str:
    """由表注释/表名推导建议实体名：去掉“…表”类后缀。"""
    base = table_comment.strip() if _has_text(table_comment) else None
    if base is None:
        return table_name or ""
    for suffix in _ENTITY_NAME_SUFFIXES:
        if len(base) > len(suffix) and base.endswith(suffix):
            return base[: -len(suffix)]
    return base


def _derive_attr_name(field_name: str | None, field_comment: str | None) -> str:
    if _has_text(field_comment):
        return field_comment.strip()
    return field_name or ""


_ATTR_NAME_INVALID = re.compile(r"[A-Za-z0-9_]")
_ATTR_NAME_CJK = re.compile(r"[\u4e00-\u9fff]")


def _attr_name_is_clean(value: str) -> bool:
    """属性建议名必须是提炼后的纯中文名：不允许字母、数字、下划线，
    且至少含一个中文字符（用户 2026-10-14 要求）。"""
    return (
        bool(value)
        and not _ATTR_NAME_INVALID.search(value)
        and bool(_ATTR_NAME_CJK.search(value))
    )


def _is_foreign_key_to(
    field_name: str | None, other_table_name: str | None, other_pk_name: str | None
) -> bool:
    """字段名为“表名_id”或与对方主键同名时视为指向该表的外键。"""
    if not _has_text(field_name):
        return False
    lower = field_name.lower()
    if lower == "id" or not lower.endswith("_id"):
        return False
    stripped = (other_table_name or "").replace("_", "").lower()
    column_stripped = lower.replace("_", "")
    if len(stripped) > 2 and column_stripped.startswith(stripped):
        return True
    return bool(other_pk_name) and lower == other_pk_name.lower()


def _build_semantic_description(
    source_name: str, target_name: str, cardinality: str, relation_name: str
) -> str:
    if cardinality == "1:1":
        return f"1个【{source_name}】{relation_name}1个【{target_name}】"
    if cardinality == "N:1":
        return (
            f"多个【{source_name}】{relation_name}1个【{target_name}】，"
            f"1个【{target_name}】被多个【{source_name}】{relation_name}"
        )
    if cardinality == "N:N":
        return f"多个【{source_name}】{relation_name}多个【{target_name}】"
    return (
        f"1个【{source_name}】{relation_name}多个【{target_name}】，"
        f"一个【{target_name}】被多个【{source_name}】{relation_name}"
    )


def _normalize_cardinality(cardinality: str | None, reversed_pair: bool) -> str:
    base = (cardinality or "1:N").strip().upper()
    if not reversed_pair:
        return base
    if base == "1:N":
        return "N:1"
    if base == "N:1":
        return "1:N"
    return base


def _split_table_column(table_column_name: str | None) -> tuple[str | None, str | None]:
    """“表名-字段名”格式拆解（兼容缺分隔符的原值）。"""
    if not _has_text(table_column_name):
        return None, None
    value = table_column_name.strip()
    idx = value.find("-")
    if idx > 0:
        return value[:idx], value[idx + 1:]
    return value, None


def _parse_llm_json_array(answer: str | None) -> list[dict[str, Any]] | None:
    """抽取模型输出里的 JSON 数组；解析失败返回 None（静默降级）。"""
    if not answer or not answer.strip():
        return None
    text = answer.strip()
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        return None
    try:
        array = json.loads(text[start:end + 1])
    except (TypeError, ValueError):
        return None
    return array if isinstance(array, list) else None


# ====================== 服务 ======================


class SemanticRecommendService:
    def __init__(
        self, reader: MetadataReader, gateway: ModelGateway | None = None,
        *, llm_enabled: bool = True,
    ) -> None:
        self._reader = reader
        self._gateway = gateway
        self._llm_enabled = llm_enabled and gateway is not None

    # ---------- 接口一：实体表推荐 ----------

    def recommend_tables(
        self, semantic_model_id: int, data_source_id: int | None,
        keyword: str | None, limit: int | None,
    ) -> list[dict[str, Any]]:
        max_size = 30 if not limit or limit <= 0 else min(limit, 200)

        # 实体唯一不能重复（2026-10-14 契约）：一张表可派生多个实体，
        # 故不按表过滤，而是过滤"建议实体名与当前模型已有实体重名"的条目
        existing_names: set[str] = {
            e["name"].strip().lower()
            for e in self._reader.entities(semantic_model_id)
            if _has_text(e.get("name"))
        }

        tables = self._reader.tables(semantic_model_id, data_source_id)
        field_counts = self._reader.field_counts(
            [row["id"] for row in tables if row.get("id") is not None]
        )

        kw = (keyword or "").strip().lower()
        matched: list[dict[str, Any]] = []
        others: list[dict[str, Any]] = []
        for table in tables:
            name = table.get("name")
            if not name:
                continue
            lower_name = name.lower()
            comment = (table.get("comment") or "").lower()
            if kw and kw not in lower_name and kw not in comment:
                continue
            suggested = _derive_entity_name(name, table.get("comment"))
            if not _has_text(suggested) or suggested.strip().lower() in existing_names:
                continue
            vo = {
                "tableId": table["id"],
                "tableName": name,
                "tableComment": table.get("comment"),
                "dataSourceId": table.get("data_source_id"),
                "suggestedEntityName": suggested,
                "fieldCount": field_counts.get(table["id"], 0),
            }
            # 关键词命中放前面；同组内按“名称命中 > 注释命中 > 有注释 > 表名”排序
            rank = (
                (0 if _has_text(table.get("comment")) else 1)
                if not kw
                else (0 if kw in lower_name else (1 if kw in comment else 2))
            )
            (matched if rank == 0 else others).append(vo)
        matched.sort(key=lambda vo: vo["tableName"])
        others.sort(key=lambda vo: vo["tableName"])
        result = matched + others[: max(0, max_size - len(matched))]
        result = result[:max_size]
        self._enhance_tables_with_llm(result, keyword)
        return self._drop_duplicate_entity_names(result, existing_names)

    def _drop_duplicate_entity_names(
        self, result: list[dict[str, Any]], existing_names: set[str]
    ) -> list[dict[str, Any]]:
        """过滤建议实体名与已有实体重名/批次内重复的条目（实体唯一）。

        大模型改写建议实体名后可能撞上已有实体名，统一在返回前剔除。
        """
        seen: set[str] = set()
        deduped: list[dict[str, Any]] = []
        for vo in result:
            key = (vo.get("suggestedEntityName") or "").strip().lower()
            if not key or key in existing_names or key in seen:
                continue
            seen.add(key)
            deduped.append(vo)
        return deduped

    def _enhance_tables_with_llm(
        self, result: list[dict[str, Any]], keyword: str | None
    ) -> None:
        """大模型按业务语义重排候选表并生成规范中文实体名；失败保留规则结果。"""
        if not self._llm_enabled or not result:
            return
        try:
            sample = result[:40]
            listing = "\n".join(
                f"{index + 1}. 表名: {vo['tableName']} | 注释: "
                f"{vo['tableComment'] or '(无注释)'} | 字段数: {vo['fieldCount']}"
                for index, vo in enumerate(sample)
            )
            keyword_hint = (
                "用户未输入关键词，请优先把主数据/核心业务事实表排在前面。"
                if not _has_text(keyword)
                else f"用户搜索关键词：{keyword.strip()}，请优先排与关键词业务含义最相关的表（含语义相关，不限于字面匹配）。"
            )
            prompt = (
                "你在为数据语义建模平台做实体映射表推荐。以下是数据源里的候选物理表清单：\n"
                + listing + "\n\n"
                + keyword_hint + "\n"
                + "任务：\n"
                + "1. 按适合作为业务实体映射表的程度从高到低排序（配置表、日志表、临时表、备份表靠后）。\n"
                + "2. 为每张表生成一个规范的中文实体名：去掉\"表/信息表/记录表/明细表\"等后缀，"
                "使用简洁的业务名词（如 comment=\"客户基本信息表\" → entityName=\"客户\"）。\n"
                + "3. 只输出 JSON 数组，不要任何解释或 markdown 代码块，"
                "格式：[{\"tableName\":\"原表名\",\"entityName\":\"中文实体名\"}]，"
                "必须覆盖输入清单中的每一张表且 tableName 原样照抄。\n"
            )
            array = _parse_llm_json_array(self._gateway.complete("实体映射表推荐排序", prompt))
            if not array:
                return
            order: dict[str, int] = {}
            entity_names: dict[str, str] = {}
            for item in array:
                if not isinstance(item, dict):
                    continue
                table_name = item.get("tableName")
                if not isinstance(table_name, str) or table_name.lower() in order:
                    continue
                order[table_name.lower()] = len(order)
                entity_name = item.get("entityName")
                if isinstance(entity_name, str) and entity_name.strip():
                    entity_names[table_name.lower()] = entity_name.strip()
            # LLM 给出的表按返回顺序重排，未返回的保持规则顺序排在后面
            result.sort(key=lambda vo: order.get(vo["tableName"].lower(), len(order)))
            for vo in result:
                better = entity_names.get(vo["tableName"].lower())
                if better:
                    vo["suggestedEntityName"] = better
        except Exception as exc:
            logger.warning("大模型增强实体表推荐失败，降级为规则排序: %s", exc)

    # ---------- 接口二：实体属性（字段）推荐 ----------

    def recommend_fields(self, table_id: int) -> list[dict[str, Any]]:
        if not table_id or table_id <= 0:
            raise SemanticRecommendError("物理表ID不能为空")
        primary: list[dict[str, Any]] = []
        normal: list[dict[str, Any]] = []
        for field in self._reader.fields(table_id):
            name = field.get("name")
            comment = field.get("comment")
            enum_values = field.get("enum_values")
            pk = bool(name) and name.lower() == "id"
            vo = {
                "fieldId": field["id"],
                "name": name,
                "comment": comment,
                "type": field.get("type"),
                "enumValues": enum_values if _has_text(enum_values) else None,
                "primaryKey": pk,
                "suggestedAttrName": _derive_attr_name(name, comment),
            }
            (primary if pk else normal).append(vo)
        normal.sort(key=lambda vo: (not _has_text(vo["comment"]), vo["name"] or ""))
        self._enhance_fields_with_llm(primary + normal)
        return primary + normal

    def _enhance_fields_with_llm(self, result: list[dict[str, Any]]) -> None:
        """大模型为属性提炼规范中文名；失败保留规则结果。

        用户 2026-10-14 要求：表/字段信息里带英文字母、数字的注释，
        必须由模型提炼成不含字母数字的新中文名，不能照抄。
        """
        if not self._llm_enabled or not result:
            return
        try:
            sample = result[:60]
            listing = "\n".join(
                f"{index + 1}. 字段名: {vo['name']} | 注释: {vo['comment'] or '(无注释)'}"
                f" | 类型: {vo['type'] or ''}"
                + (f" | 枚举值: {vo['enumValues']}" if vo["enumValues"] else "")
                for index, vo in enumerate(sample)
            )
            prompt = (
                "你在为数据语义建模平台做实体属性命名。以下是某张物理表的字段清单：\n"
                + listing + "\n\n"
                + "任务：为每个字段生成一个规范的中文属性名（attrName）：\n"
                + "1. 以注释的业务含义为准提炼；注释为空或没有业务含义时，按字段名推断业务含义。\n"
                + "2. 属性名中严禁出现英文字母、数字、下划线和英文标点，必须是纯中文（2~10 个字）。"
                "例如注释为 \"customer_level3编码\"，必须提炼为 \"客户等级编码\"，不能照抄原文。\n"
                + "3. 注释本身已是简洁规范的纯中文属性名时，原样保留，不要改写。\n"
                + "4. 只输出 JSON 数组，不要任何解释或 markdown 代码块，"
                "格式：[{\"name\":\"原字段名\",\"attrName\":\"中文属性名\"}]，"
                "必须覆盖输入清单中的每一个字段且 name 原样照抄。\n"
            )
            array = _parse_llm_json_array(
                self._gateway.complete("实体属性名提炼", prompt)
            )
            if not array:
                return
            clean_names: dict[str, str] = {}
            for item in array:
                if not isinstance(item, dict):
                    continue
                name = item.get("name")
                attr_name = item.get("attrName")
                if not isinstance(name, str) or not isinstance(attr_name, str):
                    continue
                attr_name = attr_name.strip()
                # 模型输出若仍含字母/数字则拒绝，保留规则结果
                if attr_name and _attr_name_is_clean(attr_name):
                    clean_names[name.lower()] = attr_name
            for vo in result:
                better = clean_names.get((vo["name"] or "").lower())
                if better:
                    vo["suggestedAttrName"] = better
        except Exception as exc:
            logger.warning("大模型增强属性名提炼失败，降级为规则命名: %s", exc)

    # ---------- 接口三：实体关联检测 ----------

    def detect_relations(
        self, semantic_model_id: int, entity_type_id: str,
        business_domain_id: int | None = None,
    ) -> list[dict[str, Any]]:
        if not _has_text(entity_type_id):
            raise SemanticRecommendError("实体类型ID不能为空")
        entities = self._reader.entities(semantic_model_id)
        current = next(
            (e for e in entities if e["id"] == entity_type_id), None
        )
        if current is None:
            raise SemanticRecommendError("实体类型不存在")

        # 已配置关系统计：对端实体 -> 关系数
        relations = self._reader.relations(semantic_model_id)
        configured_count: dict[str, int] = {}
        for relation in relations:
            other = None
            if entity_type_id == relation.get("source_entity_type_id"):
                other = relation.get("target_entity_type_id")
            elif entity_type_id == relation.get("target_entity_type_id"):
                other = relation.get("source_entity_type_id")
            if other:
                configured_count[other] = configured_count.get(other, 0) + 1

        # 一次 tables + 一次批量 fields 装载全部实体主表信息，
        # 替代逐实体查库（85 实体模型原实现约 173 条 SQL，实测 2.6s）
        # 业务域过滤（2026-10-14）：实体在业务域内配置（须先进入业务域才能选实体），
        # 默认按当前实体所属业务域自动过滤，避免跨域实体出现在关联提示里；
        # 当前实体无业务域时不过滤（全模型行为），显式传参可覆盖。
        if business_domain_id is None:
            business_domain_id = current.get("business_domain_id")
        others = [
            e for e in entities
            if e["id"] != entity_type_id
            and (business_domain_id is None
                 or e.get("business_domain_id") == business_domain_id)
        ]
        info_map = self._entity_table_info_map(
            [current, *others], semantic_model_id
        )
        current_info = info_map.get(entity_type_id)
        result: list[dict[str, Any]] = []
        for other in others:
            configured = configured_count.get(other["id"], 0)
            inferred = self._count_inferred_pairs(
                current, other, current_info, info_map.get(other["id"])
            )
            if configured <= 0 and inferred <= 0:
                continue
            result.append({
                "otherEntityTypeId": other["id"],
                "otherEntityName": other.get("name"),
                "related": configured > 0,
                "relationCount": configured,
                "inferred": inferred > 0,
                "inferredCount": inferred,
            })
        return result

    # ---------- 接口四：关系推荐 ----------

    def recommend_relations(
        self, semantic_model_id: int, source_entity_type_id: str,
        target_entity_type_id: str,
    ) -> list[dict[str, Any]]:
        if not _has_text(source_entity_type_id) or not _has_text(target_entity_type_id):
            raise SemanticRecommendError("源/目标实体类型ID不能为空")
        entities = self._reader.entities(semantic_model_id)
        source = next(
            (e for e in entities if e["id"] == source_entity_type_id), None
        )
        target = next(
            (e for e in entities if e["id"] == target_entity_type_id), None
        )
        if source is None or target is None:
            raise SemanticRecommendError("实体类型不存在")

        result: list[dict[str, Any]] = []

        # 1. 已配置关系（同模型、两实体间任意方向），按关系分组聚合字段对
        existing: dict[str, dict[str, Any]] = {}
        for relation in self._reader.relations(semantic_model_id):
            src_id = relation.get("source_entity_type_id")
            tgt_id = relation.get("target_entity_type_id")
            if {src_id, tgt_id} != {source_entity_type_id, target_entity_type_id}:
                continue
            reversed_pair = target_entity_type_id == src_id
            src_table, src_column = _split_table_column(relation.get("source_table_column_name"))
            tgt_table, tgt_column = _split_table_column(relation.get("target_table_column_name"))
            pair = {
                "sourceTableName": src_table,
                "sourceColumnName": src_column,
                "sourceColumnComment": None,
                "targetTableName": tgt_table,
                "targetColumnName": tgt_column,
                "targetColumnComment": None,
            }
            if reversed_pair:
                pair = {
                    "sourceTableName": pair["targetTableName"],
                    "sourceColumnName": pair["targetColumnName"],
                    "sourceColumnComment": pair["targetColumnComment"],
                    "targetTableName": pair["sourceTableName"],
                    "targetColumnName": pair["sourceColumnName"],
                    "targetColumnComment": pair["sourceColumnComment"],
                }
            group_key = "|".join([
                relation.get("name") or "",
                relation.get("category") or "",
                relation.get("type") or "",
                "R" if reversed_pair else "F",
            ])
            vo = existing.get(group_key)
            if vo is None:
                cardinality = _normalize_cardinality(relation.get("type"), reversed_pair)
                vo = {
                    "fromExisting": True,
                    "relationId": relation["id"],
                    "relationName": relation.get("name"),
                    "category": relation.get("category"),
                    "cardinality": cardinality,
                    "fieldPairs": [],
                    "semanticDescription": _build_semantic_description(
                        source["name"], target["name"], cardinality,
                        relation.get("name") or "关联",
                    ),
                }
                existing[group_key] = vo
            vo["fieldPairs"].append(pair)
        result.extend(existing.values())

        # 2. 物理层推导候选 + 大模型语义增强
        info_map = self._entity_table_info_map(
            [source, target], semantic_model_id
        )
        inferred = self._infer_relation_candidates(
            source, target,
            info_map.get(source_entity_type_id),
            info_map.get(target_entity_type_id),
        )
        self._enhance_relation_candidates_with_llm(
            source["name"], target["name"], inferred
        )
        result.extend(inferred)
        return result

    # ---------- 内部工具 ----------

    def _entity_table_info_map(
        self, entities: list[dict[str, Any]], semantic_model_id: int,
    ) -> dict[str, dict[str, Any] | None]:
        """批量装载一组实体的主表与字段信息（entity_id -> info 或 None）。

        一次 tables 查询 + 一次批量 fields 查询，替代原逐实体
        `_load_entity_table_fields`（每实体各查一次表清单和字段）。
        表匹配语义与原实现一致：按实体 data_source_id 过滤后
        取第一张与 main_table_name 同名的表。
        """
        all_tables = self._reader.tables(semantic_model_id, None)
        table_by_entity: dict[str, dict[str, Any] | None] = {}
        needed_ids: list[int] = []
        seen_ids: set[int] = set()
        for entity in entities:
            main_table_name = entity.get("main_table_name")
            if not _has_text(main_table_name):
                table_by_entity[entity["id"]] = None
                continue
            data_source_id = entity.get("data_source_id")
            table = next(
                (
                    t for t in all_tables
                    if t.get("name") == main_table_name.strip()
                    and (data_source_id is None
                         or t.get("data_source_id") == data_source_id)
                ),
                None,
            )
            table_by_entity[entity["id"]] = table
            if table and table.get("id") is not None and table["id"] not in seen_ids:
                seen_ids.add(table["id"])
                needed_ids.append(table["id"])
        fields_by_table = self._reader.fields_batch(needed_ids)
        info_map: dict[str, dict[str, Any] | None] = {}
        for entity in entities:
            table = table_by_entity.get(entity["id"])
            if not table or table.get("id") is None:
                info_map[entity["id"]] = None
                continue
            fields = fields_by_table.get(table["id"], [])
            primary = next(
                (f for f in fields if (f.get("name") or "").lower() == "id"),
                None,
            )
            info_map[entity["id"]] = {
                "tableName": table["name"], "fields": fields, "primary": primary,
            }
        return info_map

    def _count_inferred_pairs(
        self, source: dict[str, Any], target: dict[str, Any],
        source_info: dict[str, Any] | None, target_info: dict[str, Any] | None,
    ) -> int:
        candidates = self._infer_relation_candidates(
            source, target, source_info, target_info
        )
        return sum(len(candidate["fieldPairs"]) for candidate in candidates)

    def _build_pair(
        self, source_table: str, source_field: dict[str, Any],
        target_table: str, target_field: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "sourceTableName": source_table,
            "sourceColumnName": source_field.get("name"),
            "sourceColumnComment": source_field.get("comment"),
            "targetTableName": target_table,
            "targetColumnName": target_field.get("name"),
            "targetColumnComment": target_field.get("comment"),
        }

    def _build_inferred_candidate(
        self, pairs: list[dict[str, Any]], cardinality: str,
        source_name: str, target_name: str,
    ) -> dict[str, Any]:
        return {
            "fromExisting": False,
            "relationId": None,
            "relationName": "关联",
            "category": None,
            "cardinality": cardinality,
            "fieldPairs": pairs,
            "semanticDescription": _build_semantic_description(
                source_name, target_name, cardinality, "关联"
            ),
        }

    def _infer_relation_candidates(
        self, source: dict[str, Any], target: dict[str, Any],
        source_info: dict[str, Any] | None, target_info: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        """物理层推导：主键-外键命名规则（表A的id ↔ 表B的A_id）、同名字段（*_id）。

        source/target 为实体行（只用 name）；*_info 为主表信息
        （tableName/fields/primary，由 _entity_table_info_map 预构建）。
        """
        candidates: list[dict[str, Any]] = []
        if (not source_info or not target_info
                or source_info["primary"] is None
                or target_info["primary"] is None):
            return candidates
        src = source_info
        tgt = target_info

        # 源表主键 ↔ 目标表外键（源:目标 = 1:N）
        pk_fk_pairs = [
            self._build_pair(src["tableName"], src["primary"], tgt["tableName"], field)
            for field in tgt["fields"]
            if _is_foreign_key_to(
                field.get("name"), src["tableName"], src["primary"].get("name")
            )
        ]
        # 目标表主键 ↔ 源表外键（源:目标 = N:1）
        fk_pk_pairs = [
            self._build_pair(src["tableName"], field, tgt["tableName"], tgt["primary"])
            for field in src["fields"]
            if _is_foreign_key_to(
                field.get("name"), tgt["tableName"], tgt["primary"].get("name")
            )
        ]
        if pk_fk_pairs:
            candidates.append(self._build_inferred_candidate(
                pk_fk_pairs, "1:N", source["name"], target["name"]
            ))
        if fk_pk_pairs:
            candidates.append(self._build_inferred_candidate(
                fk_pk_pairs, "N:1", source["name"], target["name"]
            ))

        # 同名外键字段（共享同一维度，如两表都有 customer_id）
        same_name_pairs: dict[str, list[dict[str, Any]]] = {}
        for src_field in src["fields"]:
            src_name = src_field.get("name") or ""
            if not src_name.lower().endswith("_id") or src_name.lower() == "id":
                continue
            for tgt_field in tgt["fields"]:
                if (tgt_field.get("name") or "").lower() == src_name.lower():
                    same_name_pairs.setdefault(src_name, []).append(
                        self._build_pair(src["tableName"], src_field, tgt["tableName"], tgt_field)
                    )
        for pairs in same_name_pairs.values():
            candidates.append(self._build_inferred_candidate(
                pairs, "N:N", source["name"], target["name"]
            ))
        return candidates

    def _enhance_relation_candidates_with_llm(
        self, source_name: str, target_name: str,
        candidates: list[dict[str, Any]],
    ) -> None:
        """为物理推导候选生成语义化关系名与文档句式描述；失败保留模板结果。"""
        if not self._llm_enabled or not candidates:
            return
        try:
            listing_parts: list[str] = []
            for index, candidate in enumerate(candidates):
                listing_parts.append(f"候选{index + 1}（基数 {candidate['cardinality']}）字段对：")
                for pair in candidate["fieldPairs"]:
                    listing_parts.append(
                        f"  {pair['sourceTableName']}.{pair['sourceColumnName']}"
                        f"({pair['sourceColumnComment'] or '无注释'}) -> "
                        f"{pair['targetTableName']}.{pair['targetColumnName']}"
                        f"({pair['targetColumnComment'] or '无注释'})"
                    )
            listing = "\n".join(listing_parts)
            prompt = (
                "你在为数据语义建模平台做实体关系推荐。两个实体：源实体【" + source_name
                + "】、目标实体【" + target_name + "】。物理层推导出以下候选关联：\n"
                + listing + "\n\n"
                + "任务：为每个候选生成 1) 一个简洁贴切的中文动词关系名（如：开设、隶属、供货、包含，2-6 个字，不要泛泛的\"关联\"）；"
                + "2) 一句按基数生成的语义描述，句式示例：1:N 时为\"1个【" + source_name
                + "】<关系名>多个【" + target_name
                + "】，一个【" + target_name + "】被多个【" + source_name + "】<关系名>\"；N:1、N:N、1:1 按此规则类推。\n"
                + "只输出 JSON 数组，不要任何解释或 markdown 代码块，格式："
                + "[{\"candidate\":1,\"relationName\":\"关系名\",\"semanticDescription\":\"语义描述\"}]，"
                + "candidate 对应候选序号，必须覆盖每一个候选。\n"
            )
            array = _parse_llm_json_array(self._gateway.complete("实体关系推荐语义生成", prompt))
            if not array:
                return
            for item in array:
                if not isinstance(item, dict):
                    continue
                candidate_no = item.get("candidate")
                if not isinstance(candidate_no, int) or not 1 <= candidate_no <= len(candidates):
                    continue
                candidate = candidates[candidate_no - 1]
                relation_name = item.get("relationName")
                if isinstance(relation_name, str) and relation_name.strip():
                    candidate["relationName"] = relation_name.strip()
                    candidate["semanticDescription"] = _build_semantic_description(
                        source_name, target_name, candidate["cardinality"], relation_name.strip()
                    )
                description = item.get("semanticDescription")
                if isinstance(description, str) and description.strip():
                    candidate["semanticDescription"] = description.strip()
        except Exception as exc:
            logger.warning("大模型增强关系推荐失败，降级为模板描述: %s", exc)


# ====================== 装配 ======================

_service: SemanticRecommendService | None = None


def from_settings(settings: Any) -> SemanticRecommendService:
    gateway = None
    if settings.semantic_recommend_llm_enabled:
        gateway = WorkflowChatGateway(
            base_url=settings.model_gateway_base_url,
            api_key=(
                settings.model_gateway_api_key.get_secret_value()
                if settings.model_gateway_api_key else None
            ),
            model_name=settings.model_gateway_model_name,
            timeout_seconds=settings.model_gateway_timeout_seconds,
        )
    return SemanticRecommendService(
        PymysqlMetadataReader(
            host=settings.metadata_mysql_host,
            port=settings.metadata_mysql_port,
            user=settings.metadata_mysql_user,
            password=(
                settings.metadata_mysql_password.get_secret_value()
                if settings.metadata_mysql_password else None
            ),
            database=settings.metadata_mysql_database,
            connect_timeout=settings.metadata_mysql_connect_timeout_seconds,
            read_timeout=settings.metadata_mysql_read_timeout_seconds,
        ),
        gateway,
        llm_enabled=settings.semantic_recommend_llm_enabled,
    )


def get_service() -> SemanticRecommendService:
    global _service
    if _service is None:
        from app.config import get_settings

        _service = from_settings(get_settings())
    return _service


def reset_service() -> None:
    """测试注入用：清空缓存的单例。"""
    global _service
    _service = None
