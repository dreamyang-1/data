"""语义建模画布推荐服务（实体表/属性/关联检测/关系推荐）。

从 youoagent-api 的 SemanticModelEntityTypeServiceImpl /
SemanticModelRelationConfigServiceImpl 移植，接口契约（路径、参数、
Resp 包装、VO 字段）与 Java 版保持一致，后端可直接调用或整体切换。

- 元数据库只读查询（semantic_model_table / semantic_model_field /
  semantic_model_entity_type / semantic_model_entity_sub_table_mapping /
  semantic_model_relation_config），pymysql 按请求开短连接。
  所有查询过滤软删除行（is_deleted = 0）；Java 版由 BaseEntity 的
  MyBatis-Plus @TableLogic 处理软删除，不能把物理留存误判为删除失败。
- 大模型增强经 49 网关 /chat/workflow_chat（与 Java ChatModelBaseApi
  .translateWords 同一入口），失败或空答案静默降级为纯规则结果（留 WARNING 日志）。
  覆盖：实体表推荐（重排 + 实体名）、属性推荐（属性名提炼，不含字母数字）、
  关系推荐（关系名 + 语义描述）。
  模型名必须是网关 LLM_MODELS 支持的名称（当前为 qwen3.7-max；
  qwen3-max 不在列表，网关会静默返回空答案）。
- 全部为只读接口，不落库、不改数据。
- detect_relations 批量读取实体、关系、表、子表映射及字段；字段每 500 张表
  分批，不逐实体执行字段查询。仅使用当前模型、数据源内的有效映射。
- 两跳间接推断（2026-10-19 用户需求）：实体 A 与 C 无直接证据，但存在
  B 使 A-B、B-C 均有直接证据（已配置关系或规则命中字段对）时，生成
  经 B 的间接关系候选（VO 附 indirect/viaEntityTypeId/viaEntityName、
  中间表名 indirectTableName 与 hint 提示）。提示说明可把中间表配置为
  子表映射后直接在两端实体连线；子表映射配置完成后 recommend_relations
  会按子表字段推导出可直接填充的关系候选（附 viaSubTable/subTableNames）。
  detect_relations 不增加 SQL；recommend_relations
  仅在无已配置且无直接推导时额外批量装载全部实体映射。
  间接候选不改变"不做无中生有"契约——每一跳都必须有物理/配置证据，
  且关联检测的间接候选可被大模型复核否决。
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
    表/实体/字段/关系进入推荐结果；表与字段还须排除
    exist_status = 1（异常）的记录——四态中只有 0 存在、2 新增、
    3 恢复参与推荐，1 异常（源库中已不存在）不参与关系判断与推荐。
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
            "WHERE semantic_model_id = %s AND is_deleted = 0 "
            "AND COALESCE(exist_status, 0) <> 1"
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
            f"AND COALESCE(exist_status, 0) <> 1 "
            f"GROUP BY table_id",
            tuple(table_ids),
        )
        return {row["table_id"]: int(row["cnt"]) for row in rows}

    def fields(self, table_id: int) -> list[dict[str, Any]]:
        return self._fetch(
            "SELECT id, name, comment, type, enum_values "
            "FROM semantic_model_field "
            "WHERE table_id = %s AND is_deleted = 0 "
            "AND COALESCE(exist_status, 0) <> 1",
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
                f"WHERE table_id IN ({placeholders}) AND is_deleted = 0 "
                "AND COALESCE(exist_status, 0) <> 1",
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

    def sub_table_mappings(self, semantic_model_id: int) -> list[dict[str, Any]]:
        return self._fetch(
            "SELECT entity_type_id, sub_table_name FROM semantic_model_entity_sub_table_mapping "
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
    """按关系方向生成语义描述，只使用连线两端实体，不引入第三个实体。

    cardinality 为方向口径（1:1 / 1:N / N:1 / N:N），对外展示统一归一为
    1:1 / 1:N / M:N 三种。
    """
    if not cardinality:
        return f"【{source_name}】与【{target_name}】存在同名业务键候选，需确认业务含义及关系基数后配置"
    if cardinality == "1:1":
        return (
            f"1个【{source_name}】{relation_name}1个【{target_name}】，"
            f"1个【{target_name}】只对应1个【{source_name}】"
        )
    if cardinality == "1:N":
        return (
            f"1个【{source_name}】{relation_name}多个【{target_name}】，"
            f"1个【{target_name}】只对应1个【{source_name}】"
        )
    if cardinality == "N:1":
        return (
            f"多个【{source_name}】{relation_name}1个【{target_name}】，"
            f"1个【{target_name}】对应多个【{source_name}】"
        )
    return (
        f"多个【{source_name}】{relation_name}多个【{target_name}】，"
        f"1个【{source_name}】可{relation_name}多个【{target_name}】，"
        f"1个【{target_name}】也可被多个【{source_name}】{relation_name}"
    )


def _display_cardinality(cardinality: str | None) -> str:
    """推荐关系对外只展示 1:1 / 1:N / M:N 三种。

    方向口径 N:1（源端为多）归入一对多，N:N 记为 M:N；无法确认时保持空串。
    """
    value = (cardinality or "").strip().upper()
    if value in ("1:1", "1:N"):
        return value
    if value == "N:1":
        return "1:N"
    if value in ("N:N", "M:N"):
        return "M:N"
    return ""


def _relation_name_is_clean(value: str | None) -> bool:
    """大模型关系名合法性：2-6 字、不含【】/换行、不能是泛化的“关联/关系”。"""
    if not isinstance(value, str):
        return False
    name = value.strip()
    if not 2 <= len(name) <= 6:
        return False
    if any(ch in name for ch in "【】\n\r"):
        return False
    return name not in ("关联", "关系")


def _references_table(column: str | None, table_name: str | None, pk_name: str | None) -> bool:
    """字段名是否自证指向某张表：列名以表名开头，或与对方主键同名。"""
    name = (column or "").lower()
    if not name:
        return False
    table = (table_name or "").replace("_", "").lower()
    if len(table) > 2 and name.replace("_", "").startswith(table):
        return True
    return bool(pk_name) and name == (pk_name or "").lower()


def _same_key_cardinality(source_refs_target: bool, target_refs_source: bool) -> str:
    """同名业务键的基数推断（元数据看不到唯一索引，只能按表名自证引用方向）。

    - 只有目标端字段指向源表（如 sales_order.customer_code → customer）→ 1:N；
    - 只有源端字段指向目标表 → N:1；
    - 两端互为引用 → 1:1；
    - 两端都指向第三个业务维度（表名互不匹配）→ N:N。
    """
    if source_refs_target and target_refs_source:
        return "1:1"
    if target_refs_source:
        return "1:N"
    if source_refs_target:
        return "N:1"
    return "N:N"


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


def _valid_field_pairs(value: Any) -> list[dict[str, str]]:
    """校验模型输出的关联字段对：四个表名/字段名键齐全且非空。"""
    pairs: list[dict[str, str]] = []
    if not isinstance(value, list):
        return pairs
    keys = (
        "sourceTableName", "sourceColumnName",
        "targetTableName", "targetColumnName",
    )
    for item in value:
        if not isinstance(item, dict):
            continue
        if all(isinstance(item.get(k), str) and item[k].strip() for k in keys):
            pairs.append({k: item[k].strip() for k in keys})
    return pairs


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
        evidence: dict[str, list[dict[str, Any]]] = {}
        direct_by_id: dict[str, list[dict[str, Any]]] = {}
        skipped: list[dict[str, Any]] = []
        for other in others:
            configured = configured_count.get(other["id"], 0)
            candidates = self._infer_relation_candidates(
                current, other, current_info, info_map.get(other["id"])
            )
            direct_by_id[other["id"]] = candidates
            inferred = sum(len(c["fieldPairs"]) for c in candidates)
            if configured <= 0 and inferred <= 0:
                skipped.append(other)
                continue
            evidence[other["id"]] = candidates
            result.append({
                "otherEntityTypeId": other["id"],
                "otherEntityName": other.get("name"),
                "related": configured > 0,
                "relationCount": configured,
                "inferred": inferred > 0,
                "inferredCount": inferred,
            })
        # 两跳间接推断（2026-10-19 用户需求）：A-C 无直接证据，但存在 B 使
        # A-B、B-C 均有直接证据 → 生成经 B 的间接候选（纯内存计算，不加 SQL）。
        indirect = self._infer_indirect_candidates(
            current, current_info, skipped, others, relations, info_map,
            direct_by_id=direct_by_id,
        )
        for vo in indirect:
            evidence.setdefault(vo["otherEntityTypeId"], []).append(vo)
            result.append({
                "otherEntityTypeId": vo["otherEntityTypeId"],
                "otherEntityName": vo.get("otherEntityName"),
                "related": False,
                "relationCount": 0,
                "inferred": False,
                "inferredCount": 0,
                "indirect": True,
                "viaEntityTypeId": vo.get("viaEntityTypeId"),
                "viaEntityName": vo.get("viaEntityName"),
                "indirectTableName": vo.get("indirectTableName"),
                "hint": vo.get("hint"),
                "semanticDescription": vo.get("semanticDescription"),
                "relationCandidates": [vo],
            })
        # 大模型复核（2026-10-15）：确认/否决/修正规则命中的字段对并排序；
        # 失败时静默降级，result 保持纯规则结果与原顺序。
        # 2026-10-15 用户契约：模型不做无中生有的建议——未配置且规则未命中
        # 的实体对一律不返回（间接候选每跳均有证据，且可被模型否决）。
        self._enhance_detect_relations_with_llm(
            current, current_info, result, evidence, info_map
        )
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

        relations = self._reader.relations(semantic_model_id)
        info_map = self._entity_table_info_map([source, target], semantic_model_id)

        def resolve_field(value: str | None, entity_id: str) -> tuple[str | None, str | None]:
            table, column = _split_table_column(value)
            if column:
                return table, column
            # 旧记录可能只保存列名。只在对应实体主表中精确匹配，不猜表名。
            info = info_map.get(entity_id)
            if info and table:
                matches = [(item["tableName"], f["name"])
                           for item in [info, *info.get("mappedTables", [])]
                           for f in item["fields"] if f.get("name") == table]
                if len(matches) == 1:
                    return matches[0]
            return None, None

        result: list[dict[str, Any]] = []

        # 1. 已配置关系（同模型、两实体间任意方向），按关系分组聚合字段对
        existing: dict[str, dict[str, Any]] = {}
        for relation in relations:
            src_id = relation.get("source_entity_type_id")
            tgt_id = relation.get("target_entity_type_id")
            if {src_id, tgt_id} != {source_entity_type_id, target_entity_type_id}:
                continue
            reversed_pair = target_entity_type_id == src_id
            src_table, src_column = resolve_field(relation.get("source_table_column_name"), src_id)
            tgt_table, tgt_column = resolve_field(relation.get("target_table_column_name"), tgt_id)
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
            if pair not in vo["fieldPairs"]:
                vo["fieldPairs"].append(pair)
        result.extend(existing.values())

        # 2. 物理层推导候选 + 大模型语义增强
        inferred = self._infer_relation_candidates(
            source, target,
            info_map.get(source_entity_type_id),
            info_map.get(target_entity_type_id),
        )
        self._enhance_relation_candidates_with_llm(
            source["name"], target["name"], inferred
        )
        # 相同物理字段优先采用已配置关系的基数，避免并列展示相互矛盾的推断。
        for candidate in inferred:
            for configured in existing.values():
                keys = ("sourceTableName", "sourceColumnName", "targetTableName", "targetColumnName")
                signature = lambda pairs: {tuple(p.get(k) for k in keys) for p in pairs}
                if (signature(candidate["fieldPairs"]) == signature(configured["fieldPairs"])
                        and candidate["cardinality"] != configured["cardinality"]):
                    candidate["cardinality"] = configured["cardinality"]
                    # 描述按候选自身关系名重建，避免串用已配置关系的名称
                    candidate["semanticDescription"] = _build_semantic_description(
                        source["name"], target["name"],
                        configured["cardinality"], candidate["relationName"],
                    )
                    break
        result.extend(inferred)

        # 3. 两跳间接推断（2026-10-19 用户需求）：A 与 C 无已配置且无直接
        # 推导时，搜索中间实体 B（A-B、B-C 均有直接证据）生成经 B 的候选。
        # 惰性触发：仅在直接结果为空时额外装载全部实体（+2 条 SQL）。
        if not result:
            all_others = [
                e for e in entities if e["id"] != source_entity_type_id
            ]
            all_info = self._entity_table_info_map(
                [source, *all_others], semantic_model_id
            )
            indirect = self._infer_indirect_candidates(
                source, all_info.get(source_entity_type_id),
                [target], all_others, relations, all_info,
            )
            result.extend(indirect)
        # 对外只展示 1:1 / 1:N / M:N 三种关系类型（用户 2026-10-22 要求）
        for vo in result:
            vo["cardinality"] = _display_cardinality(vo.get("cardinality"))
            # 关系分类：平台统一使用「关联关系」，已配置行缺失分类时补齐
            vo["category"] = vo.get("category") or "关联关系"
        return result

    # ---------- 内部工具 ----------

    def _entity_table_info_map(
        self, entities: list[dict[str, Any]], semantic_model_id: int,
    ) -> dict[str, dict[str, Any] | None]:
        """批量装载一组实体的主表与字段信息（entity_id -> info 或 None）。

        tables/sub_table_mappings 查询 + 批量 fields 查询，替代原逐实体
        `_load_entity_table_fields`（每实体各查一次表清单和字段）。
        表匹配语义与原实现一致：按实体 data_source_id 过滤后
        取第一张与 main_table_name 同名的表。
        """
        all_tables = self._reader.tables(semantic_model_id, None)
        load_mappings = getattr(self._reader, "sub_table_mappings", None)
        mappings = load_mappings(semantic_model_id) if load_mappings else []
        mapped_tables: dict[str, list[dict[str, Any]]] = {}
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
            names = {m.get("sub_table_name") for m in mappings if m.get("entity_type_id") == entity["id"]}
            mapped_tables[entity["id"]] = [
                t for t in all_tables
                if t.get("name") in names and t.get("name") != main_table_name.strip()
                and (data_source_id is None or t.get("data_source_id") == data_source_id)
            ]
            for mapped in mapped_tables[entity["id"]]:
                if mapped.get("id") is not None and mapped["id"] not in seen_ids:
                    seen_ids.add(mapped["id"])
                    needed_ids.append(mapped["id"])
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
            if mapped_tables.get(entity["id"]):
                info_map[entity["id"]]["mappedTables"] = [
                    {
                        "tableName": t["name"],
                        "fields": fields_by_table.get(t["id"], []),
                        "primary": next((f for f in fields_by_table.get(t["id"], [])
                                         if (f.get("name") or "").lower() == "id"), None),
                    }
                    for t in mapped_tables[entity["id"]]
                ]
        return info_map

    def _enhance_detect_relations_with_llm(
        self, current: dict[str, Any], current_info: dict[str, Any] | None,
        result: list[dict[str, Any]],
        evidence: dict[str, list[dict[str, Any]]],
        info_map: dict[str, dict[str, Any] | None],
    ) -> None:
        """大模型复核关联检测（2026-10-15）：对规则已命中的对核对/修正字段
        对并排序。模型不做无中生有的建议——只复核 result 中已存在的对
        （已配置或规则命中），未配置且规则未命中的对一律不返回。

        单次批量调用覆盖全部候选对；网关失败或输出不可解析时静默降级，
        result 保持纯规则结果与原顺序。新增字段：
        llmReviewed / llmConfirmed / llmCardinality / llmFieldPairs /
        llmConfidence / llmReason。
        """
        if not self._llm_enabled or not result or current_info is None:
            return

        def schema_block(name: str, info: dict[str, Any] | None) -> str:
            if info is None:
                return f"  实体【{name}】：无登记主表信息"
            fields = info["fields"][:40]
            lines = [
                f"  {f.get('name')}({f.get('comment') or '无注释'})"
                for f in fields
            ]
            more = "" if len(info["fields"]) <= 40 else "\n  ...(其余字段省略)"
            pk = info["primary"].get("name") if info["primary"] else "无"
            return (
                f"  实体【{name}】主表 {info['tableName']}，主键 {pk}，字段：\n"
                + "\n".join(lines) + more
            )

        try:
            by_id = {vo["otherEntityTypeId"]: vo for vo in result}
            blocks: list[str] = []
            for vo in result:
                other_info = info_map.get(vo["otherEntityTypeId"])
                other_name = vo.get("otherEntityName") or vo["otherEntityTypeId"]
                block = schema_block(other_name, other_info)
                if vo.get("indirect"):
                    block += (
                        f"\n  （该候选为经由中间实体【{vo.get('viaEntityName') or '未知'}】"
                        "的两跳间接关联，字段对跨越中间表，请结合业务判断是否成立）"
                    )
                if vo["related"]:
                    block += f"\n  （已有 {vo['relationCount']} 条人工配置的关联关系）"
                candidates = evidence.get(vo["otherEntityTypeId"]) or []
                if candidates:
                    pair_lines = [
                        f"    {pair['sourceTableName']}.{pair['sourceColumnName']}"
                        f" -> {pair['targetTableName']}.{pair['targetColumnName']}"
                        for candidate in candidates
                        for pair in candidate["fieldPairs"]
                    ]
                    block += "\n  规则推导出的可关联字段对：\n" + "\n".join(pair_lines)
                else:
                    block += "\n  规则未推导出可关联字段对。"
                blocks.append(block)
            prompt = (
                "你在为数据语义建模平台复核实体间的关联关系。当前拖入实体：\n"
                + schema_block(current.get("name") or "", current_info)
                + "\n\n候选关联实体（共 " + str(len(result)) + " 个）：\n"
                + "\n\n".join(blocks)
                + "\n\n任务：对每个候选实体判断它与当前实体是否存在业务关联：\n"
                + "1. 核对规则推导出的字段对是否有真实业务对应意义，可修正或补充字段对。\n"
                + "2. cardinality 取值：1:1、1:N、N:1、N:N（以当前实体为源）。\n"
                + "3. confidence 取值：high/medium/low。\n"
                + "只输出 JSON 数组，不要任何解释或 markdown 代码块，格式："
                + "[{\"otherEntityTypeId\":\"候选实体ID\",\"confirmed\":true,"
                "\"cardinality\":\"1:N\",\"confidence\":\"high\","
                "\"reason\":\"一句话理由\","
                "\"fieldPairs\":[{\"sourceTableName\":\"表名\","
                "\"sourceColumnName\":\"字段名\",\"targetTableName\":\"表名\","
                "\"targetColumnName\":\"字段名\"}]}]，"
                + "必须覆盖每一个候选实体。\n"
            )
            array = _parse_llm_json_array(
                self._gateway.complete("实体关联检测复核", prompt)
            )
            if not array:
                return
            for item in array:
                if not isinstance(item, dict):
                    continue
                other_id = item.get("otherEntityTypeId")
                vo = by_id.get(other_id) if isinstance(other_id, str) else None
                # 只复核已存在的对：未知实体ID的输出忽略（不做新建议）
                if vo is None:
                    continue
                pairs = _valid_field_pairs(item.get("fieldPairs"))[:10]
                vo["llmReviewed"] = True
                vo["llmConfirmed"] = bool(item.get("confirmed"))
                vo["llmConfidence"] = (
                    item.get("confidence")
                    if item.get("confidence") in ("high", "medium", "low")
                    else "medium"
                )
                reason = item.get("reason")
                vo["llmReason"] = (
                    reason.strip() if isinstance(reason, str) and reason.strip() else None
                )
                cardinality = item.get("cardinality")
                vo["llmCardinality"] = (
                    cardinality if cardinality in ("1:1", "1:N", "N:1", "N:N") else None
                )
                vo["llmFieldPairs"] = pairs
            # 过滤（2026-10-15 用户契约）：没有关联关系的对不返回——
            # 模型否决且无已配置关系的对整体移除
            result[:] = [
                vo for vo in result
                if vo.get("related") or vo.get("llmConfirmed") is not False
            ]
            # 排序：已配置 > 规则命中且模型未否决
            conf_rank = {"high": 0, "medium": 1, "low": 2}

            def sort_key(vo: dict[str, Any]) -> tuple:
                tier = 0 if vo.get("related") else 1
                return (
                    tier, conf_rank.get(vo.get("llmConfidence"), 1),
                    -vo.get("inferredCount", 0),
                )

            result.sort(key=sort_key)
        except Exception as exc:
            logger.warning("大模型复核关联检测失败，降级为规则结果: %s", exc)

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
            "category": "关联关系",
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
        """按当前实体的物理字段推导候选，不依赖已删除的关系配置。

        source/target 为实体行（只用 name）；*_info 为主表信息
        （tableName/fields/primary，由 _entity_table_info_map 预构建）。
        """
        candidates: list[dict[str, Any]] = []
        if not source_info or not target_info:
            return candidates
        if (source.get("data_source_id") is not None
                and target.get("data_source_id") is not None
                and source["data_source_id"] != target["data_source_id"]):
            return candidates
        src = source_info
        tgt = target_info
        if src.get("mappedTables") or tgt.get("mappedTables"):
            seen: set[tuple] = set()
            for source_table in [src, *src.get("mappedTables", [])]:
                for target_table in [tgt, *tgt.get("mappedTables", [])]:
                    for candidate in self._infer_relation_candidates(
                        source, target,
                        {k: v for k, v in source_table.items() if k != "mappedTables"},
                        {k: v for k, v in target_table.items() if k != "mappedTables"},
                    ):
                        signature = tuple(tuple(p[k] for k in (
                            "sourceTableName", "sourceColumnName", "targetTableName", "targetColumnName"
                        )) for p in candidate["fieldPairs"])
                        if signature not in seen:
                            seen.add(signature)
                            # 子表映射参与推导：标记关系来自哪张子表，便于连线时直接填充
                            sub_tables = [
                                item["tableName"]
                                for item, main in ((source_table, src), (target_table, tgt))
                                if item is not main
                            ]
                            if sub_tables:
                                candidate["viaSubTable"] = True
                                candidate["subTableNames"] = sub_tables
                            candidates.append(candidate)
            return candidates

        # 源表主键 ↔ 目标表外键（源:目标 = 1:N）
        pk_fk_pairs = [
            self._build_pair(src["tableName"], src["primary"], tgt["tableName"], field)
            for field in tgt["fields"]
            if src["primary"] and _is_foreign_key_to(
                field.get("name"), src["tableName"], src["primary"].get("name")
            )
            and not any(f.get("name") == field.get("name") for f in src["fields"])
        ]
        # 目标表主键 ↔ 源表外键（源:目标 = N:1）
        fk_pk_pairs = [
            self._build_pair(src["tableName"], field, tgt["tableName"], tgt["primary"])
            for field in src["fields"]
            if tgt["primary"] and _is_foreign_key_to(
                field.get("name"), tgt["tableName"], tgt["primary"].get("name")
            )
            and not any(f.get("name") == field.get("name") for f in tgt["fields"])
        ]
        for pair in pk_fk_pairs:
            candidates.append(self._build_inferred_candidate(
                [pair], "1:N", source["name"], target["name"]
            ))
        for pair in fk_pk_pairs:
            candidates.append(self._build_inferred_candidate(
                [pair], "N:1", source["name"], target["name"]
            ))

        # 同名业务键也能在删除/重建实体后重新推荐；基数按“列名是否指向对方表”
        # 推断方向，两侧都指向同一外部业务维度时按多对多推荐。
        src_pk_name = (src.get("primary") or {}).get("name")
        tgt_pk_name = (tgt.get("primary") or {}).get("name")
        same_name_pairs: dict[str, list[dict[str, Any]]] = {}
        same_name_flags: dict[str, set[tuple[bool, bool]]] = {}
        for src_field in src["fields"]:
            src_name = src_field.get("name") or ""
            if not src_name.lower().endswith(("_id", "_code", "_no", "_key")):
                continue
            for tgt_field in tgt["fields"]:
                tgt_name = tgt_field.get("name") or ""
                if tgt_name.lower() == src_name.lower():
                    def type_family(field: dict[str, Any]) -> str:
                        field_type = (field.get("type") or "").lower().split("(")[0].strip()
                        if field_type in {"char", "varchar", "text", "string", "nvarchar", "nchar"}:
                            return "text"
                        if field_type in {"tinyint", "smallint", "int", "integer", "bigint", "decimal", "numeric", "number"}:
                            return "number"
                        return field_type
                    left_type, right_type = type_family(src_field), type_family(tgt_field)
                    if left_type and right_type and left_type != right_type:
                        continue
                    same_name_pairs.setdefault(src_name, []).append(
                        self._build_pair(src["tableName"], src_field, tgt["tableName"], tgt_field)
                    )
                    same_name_flags.setdefault(src_name, set()).add((
                        _references_table(src_name, tgt["tableName"], tgt_pk_name),
                        _references_table(tgt_name, src["tableName"], src_pk_name),
                    ))
        for src_name, pairs in same_name_pairs.items():
            flags = same_name_flags.get(src_name) or set()
            cardinality = _same_key_cardinality(*next(iter(flags))) if len(flags) == 1 else "N:N"
            candidates.append(self._build_inferred_candidate(
                pairs, cardinality, source["name"], target["name"]
            ))
        return candidates

    # ---------- 两跳间接推断（2026-10-19 用户需求） ----------

    @staticmethod
    def _merge_cardinality(first_hop: str | None, second_hop: str | None) -> str:
        """两跳基数合成：A→B→C 推 A:C 的多重性。

        链式乘法语义（"1:N" = 1 个源行对 N 个目标行）：
        - C 侧为 N ⟺ 1 个 A 行能展开到多个 C 行：第一跳 A:B 的 B 侧为 N
          （1 个 A 对多个 B），或第二跳 B:C 的 C 侧为 N（1 个 B 对多个 C）；
        - A 侧为 N ⟺ 1 个 C 行能展开到多个 A 行：第二跳 B 侧为 N
          （N 个 B 对 1 个 C，1 个 C 关联多个 B 行），或第一跳 A 侧为 N
          （N 个 A 对 1 个 B，该 B 的多个 A 行各自可达 C）。
        例：1:N（A→B）+ N:1（B→C）→ N:N（经典中间表场景）；
            N:1（A→B）+ 1:N（B→C）→ N:N。
        """
        first = _normalize_cardinality(first_hop, False)
        second = _normalize_cardinality(second_hop, False)
        a_mult = "N" if (first.startswith("N") or second.startswith("N")) else "1"
        c_mult = "N" if (first.endswith("N") or second.endswith("N")) else "1"
        if a_mult == "N" and c_mult == "1":
            return "N:1"
        if a_mult == "1" and c_mult == "N":
            return "1:N"
        return f"{a_mult}:{c_mult}"

    def _configured_hops(
        self, source: dict[str, Any], target: dict[str, Any],
        relations: list[dict[str, Any]],
    ) -> list[tuple[list[dict[str, Any]], str]]:
        """已配置关系作为一跳证据：返回 [(字段对列表, 基数)]，方向统一 source→target。"""
        hops: list[tuple[list[dict[str, Any]], str]] = []
        for relation in relations:
            src_id = relation.get("source_entity_type_id")
            tgt_id = relation.get("target_entity_type_id")
            if {src_id, tgt_id} != {source["id"], target["id"]}:
                continue
            reversed_pair = target["id"] == src_id
            src_table, src_column = _split_table_column(
                relation.get("source_table_column_name")
            )
            tgt_table, tgt_column = _split_table_column(
                relation.get("target_table_column_name")
            )
            if reversed_pair:
                src_table, src_column, tgt_table, tgt_column = (
                    tgt_table, tgt_column, src_table, src_column,
                )
            if not (src_table and src_column and tgt_table and tgt_column):
                continue
            hops.append(([{
                "sourceTableName": src_table,
                "sourceColumnName": src_column,
                "sourceColumnComment": None,
                "targetTableName": tgt_table,
                "targetColumnName": tgt_column,
                "targetColumnComment": None,
            }], _normalize_cardinality(relation.get("type"), reversed_pair)))
        return hops

    def _build_indirect_description(
        self, source_name: str, via_name: str | None, table_name: str | None,
        target_name: str, cardinality: str,
    ) -> str:
        """间接候选语义描述：明确写出中间表（有表名时）与中间实体。"""
        if _has_text(table_name):
            middle = f"中间表【{table_name}】"
            if _has_text(via_name):
                middle += f"（实体【{via_name}】）"
        elif _has_text(via_name):
            middle = f"中间实体【{via_name}】"
        else:
            middle = "中间实体"
        if cardinality == "1:1":
            return f"1个【{source_name}】通过{middle}间接关联1个【{target_name}】"
        if cardinality == "N:1":
            return f"多个【{source_name}】通过{middle}间接关联1个【{target_name}】"
        if cardinality == "1:N":
            return f"1个【{source_name}】通过{middle}间接关联多个【{target_name}】"
        return f"多个【{source_name}】通过{middle}间接关联多个【{target_name}】"

    def _build_indirect_hint(
        self, source_name: str, via_name: str | None, table_name: str | None,
        target_name: str,
    ) -> str:
        """间接候选的操作提示：怎样把它变成可直接连线的真实关系。"""
        if _has_text(table_name):
            return (
                f"可通过中间表【{table_name}】建立关联：把该表配置为"
                f"【{source_name}】或【{target_name}】的子表映射后，"
                f"直接连接【{source_name}】与【{target_name}】即可自动填充关系"
            )
        middle = f"中间实体【{via_name}】" if _has_text(via_name) else "中间实体"
        return (
            f"可通过{middle}建立关联：为中间实体补充子表映射后，"
            f"直接连接【{source_name}】与【{target_name}】即可自动填充关系"
        )

    def _infer_indirect_candidates(
        self, current: dict[str, Any], current_info: dict[str, Any] | None,
        targets: list[dict[str, Any]], others: list[dict[str, Any]],
        relations: list[dict[str, Any]],
        info_map: dict[str, dict[str, Any] | None],
        direct_by_id: dict[str, list[dict[str, Any]]] | None = None,
        *, max_per_target: int = 1, max_total: int = 8,
    ) -> list[dict[str, Any]]:
        """两跳间接候选：A-C 无直接证据，但存在 B 使 A-B、B-C 均有直接证据
        （已配置关系或规则命中字段对）→ 生成经 B 的候选。

        纯内存计算，不触发任何 SQL。每个对端默认取最先命中的一个中间实体
        （bridge 顺序与 others 一致，保证稳定），全局上限 max_total 防爆炸。
        direct_by_id 可传入调用方已算好的 current→X 规则候选复用（bridge 也在
        others 中，第一轮循环已计算）；未传时内部重算。
        """
        if current_info is None or not targets:
            return []
        target_ids = {t["id"] for t in targets}
        # bridge 池：除当前实体与目标外、与 current 存在一跳证据的实体
        bridge_map: dict[str, tuple[dict[str, Any], list[tuple[list, str]]]] = {}
        for entity in others:
            if entity["id"] == current["id"] or entity["id"] in target_ids:
                continue
            hops = self._configured_hops(current, entity, relations)
            rule_candidates = (direct_by_id or {}).get(entity["id"])
            if rule_candidates is None:
                entity_info = info_map.get(entity["id"])
                rule_candidates = (
                    self._infer_relation_candidates(
                        current, entity, current_info, entity_info
                    )
                    if entity_info is not None else []
                )
            hops.extend(
                (c["fieldPairs"], c["cardinality"])
                for c in rule_candidates if c["cardinality"]
            )
            if hops:
                bridge_map[entity["id"]] = (entity, hops)
        if not bridge_map:
            return []

        result: list[dict[str, Any]] = []
        for target in targets:
            if len(result) >= max_total:
                break
            target_info = info_map.get(target["id"])
            if target_info is None:
                continue
            added = 0
            for bridge_id, (bridge, first_hops) in bridge_map.items():
                if added >= max_per_target or len(result) >= max_total:
                    break
                second_hops: list[tuple[list, str]] = self._configured_hops(
                    bridge, target, relations
                )
                bridge_info = info_map.get(bridge_id)
                if bridge_info is not None:
                    second_hops.extend(
                        (c["fieldPairs"], c["cardinality"])
                        for c in self._infer_relation_candidates(
                            bridge, target, bridge_info, target_info
                        ) if c["cardinality"]
                    )
                if not second_hops:
                    continue
                first_pairs, first_card = first_hops[0]
                second_pairs, second_card = second_hops[0]
                pairs = [*first_pairs, *second_pairs]
                if not pairs:
                    continue
                cardinality = self._merge_cardinality(first_card, second_card)
                vo = self._build_inferred_candidate(
                    pairs, cardinality, current["name"], target["name"]
                )
                vo["relationName"] = "间接关联"
                bridge_table = (bridge_info or {}).get("tableName")
                vo["semanticDescription"] = self._build_indirect_description(
                    current["name"], bridge.get("name"), bridge_table,
                    target["name"], cardinality,
                )
                vo["indirect"] = True
                vo["viaEntityTypeId"] = bridge_id
                vo["viaEntityName"] = bridge.get("name")
                vo["indirectTableName"] = bridge_table
                vo["hint"] = self._build_indirect_hint(
                    current["name"], bridge.get("name"), bridge_table,
                    target["name"],
                )
                vo["otherEntityTypeId"] = target["id"]
                vo["otherEntityName"] = target.get("name")
                result.append(vo)
                added += 1
        return result

    def _enhance_relation_candidates_with_llm(
        self, source_name: str, target_name: str,
        candidates: list[dict[str, Any]],
    ) -> None:
        """为物理推导候选生成语义化关系名；描述始终由模板确定性生成。

        模型只负责重命名（2-6 字动词），不再采用模型生成的语义描述，
        避免描述里混入第三个实体；失败或关系名不合法时保留模板结果。
        """
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
                + "任务：为每个候选生成一个简洁贴切的中文动词关系名（如：开设、隶属、供货、包含，2-6 个字，不要泛泛的\"关联\"）。"
                + "关系名只能描述这两个实体之间的动作，禁止提到任何第三个实体。\n"
                + "只输出 JSON 数组，不要任何解释或 markdown 代码块，格式："
                + "[{\"candidate\":1,\"relationName\":\"关系名\"}]，"
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
                if _relation_name_is_clean(relation_name):
                    name = relation_name.strip()
                    old_name = candidate.get("relationName") or "关联"
                    description = candidate.get("semanticDescription")
                    candidate["relationName"] = name
                    if isinstance(description, str) and description:
                        candidate["semanticDescription"] = description.replace(old_name, name)
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
