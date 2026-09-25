"""Unbound planner input and literal handling; these helpers grant no scope."""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import re


def literal_key(value):
    return re.sub(r"\s+", "", str(value if value is not None else "")).casefold()


def compact_literal(value):
    """Numbers, identifiers and Latin names, including mixed-script model names."""
    return isinstance(value, (int, float, bool)) or bool(
        isinstance(value, str) and re.search(r"[A-Za-z0-9]", value)
    )


def literal_values(value):
    return value if isinstance(value, list) else [value]


def compatible_literal_match(mention, canonical, match_type):
    if literal_key(mention) == literal_key(canonical):
        return True
    token_pattern = r"[A-Za-z0-9]+(?:[-_./][A-Za-z0-9]+)*"
    left = re.findall(token_pattern, str(mention).casefold())
    right = re.findall(token_pattern, str(canonical).casefold())
    if not left or not right:
        return False
    if left == right:
        return True  # e.g. 品牌Model M60 set -> Model M60 set
    return match_type == "CANONICAL_CONTAINS_MENTION" and any(
        right[i:i + len(left)] == left for i in range(len(right) - len(left) + 1)
    )


def numeric_literal(value):
    """Parse a numeric measure, never an identifier; no inferred unit conversion."""
    if isinstance(value, bool):
        return value
    text = str(value).strip()
    if "," in text:
        if not re.fullmatch(r"[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?", text):
            raise ValueError("invalid thousands separator")
        text = text.replace(",", "")
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError("not a numeric literal") from exc
    if not number.is_finite() or abs(number.adjusted()) > 100:
        raise ValueError("non-finite or oversized numeric literal")
    return int(number) if number == number.to_integral() else format(number, "f")


def planner_reference(extraction, fallback):
    """Consume the already-sent planner JSON, not stale classifier role guesses.

    Keep this advisory: downstream must independently ground every field.
    Do not copy scope/authorization from either extraction or its entity names.
    """
    if not isinstance(extraction, dict) or "意图" not in extraction:
        return deepcopy(fallback)
    if not isinstance(extraction.get("意图"), str):
        return deepcopy(fallback)
    result = {}
    result["primary_intent"] = {
        "明细查询": "DETAIL_QUERY",
    }.get(extraction.get("意图"), extraction.get("意图"))
    entities = extraction.get("实体") or []
    if isinstance(entities, list) and entities and isinstance(entities[0], str):
        result["entity"] = entities[0]
    result["metrics"] = [
        {"input": item} if isinstance(item, str) else deepcopy(item)
        for item in extraction.get("指标") or [] if isinstance(item, (str, dict))
    ]
    result["dimensions"] = [v for v in extraction.get("维度") or [] if isinstance(v, str)]
    result["fields"] = [
        item["field"] for item in extraction.get("展示字段") or []
        if isinstance(item, dict) and isinstance(item.get("field"), str)
    ]
    result["filters"] = []
    aliases = {"EQ": "=", "NE": "!=", "NEQ": "!=", "GT": ">", "GTE": ">=",
               "LT": "<", "LTE": "<=", "NOT_IN": "NOT IN"}
    for item in extraction.get("过滤条件") or []:
        if not isinstance(item, dict) or not isinstance(item.get("field"), str):
            continue
        operator = str(item.get("op") or item.get("operator") or "=").upper()
        operator = aliases.get(operator, operator)
        value = deepcopy(item.get("value"))
        if isinstance(value, list) and len(value) == 1 and operator not in {"IN", "NOT IN", "BETWEEN"}:
            value = value[0]
        elif isinstance(value, list) and len(value) > 1 and operator in {"=", "!="}:
            operator = "IN" if operator == "=" else "NOT IN"
        result["filters"].append({"field": item["field"], "operator": operator, "value": value})
    return result
