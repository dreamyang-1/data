"""Bounded representation repair; never infer user parameters or relax scope."""
from copy import deepcopy

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict


class SlotNameCorrections(BaseModel):
    model_config = ConfigDict(extra='forbid')
    replacements: dict[str, str | None]


SLOT_NAME_RULES = '''
operation_markers.slot_name 和 explicit_slot_mentions 的键只能使用 slots 中的内部槽位名，
不是 candidate_roles、业务名称或输出类型。subject=业务对象，metrics=指标，dimensions=分组，
projection_spec=明细返回字段，filter_expression=筛选，time_spec=时间，ranking_spec=排序/数量，
comparison_spec=对比，delivery_spec=交付格式，relationship_spec=关系。
请求明细与请求汇总的区别由 query_shape_prediction 表达；只有实际字段编辑才产生槽位操作，
不要发明 detail、query_type、query_shape、display_fields 等槽位。结合本轮问题和上下文判断，
不要仅凭某个词就决定操作类型，也不要凭空补充返回字段。
'''


def repairable_slot_names(raw, schema):
    """Only bad slot vocabulary qualifies, never IDs, values or other errors."""
    errors = list(Draft202012Validator(schema).iter_errors(raw))
    allowed = schema.get('$defs', {}).get('OperationMarker', {}).get('properties', {}).get('slot_name', {}).get('enum', [])
    if not errors or not allowed or not isinstance(raw, dict):
        return None
    invalid = set()
    for error in errors:
        path = list(error.absolute_path)
        if (error.validator == 'enum' and len(path) == 3
                and path[0] == 'operation_markers' and isinstance(path[1], int)
                and path[2] == 'slot_name' and isinstance(error.instance, str)):
            invalid.add(error.instance)
        elif (error.validator == 'additionalProperties' and path == ['explicit_slot_mentions']
                and isinstance(error.instance, dict)):
            invalid.update(set(error.instance) - set(allowed))
        else:
            return None
    if not invalid or len(invalid) > 10:
        return None
    return sorted(invalid), allowed


def correction_schema(invalid, allowed):
    schema = SlotNameCorrections.model_json_schema()
    schema['properties']['replacements'] = {
        'type': 'object', 'properties': {name: {'enum': [*allowed, None]} for name in invalid},
        'required': invalid, 'additionalProperties': False,
    }
    return schema


def apply_slot_names(raw, replacements):
    """Rename only. A null, collision or inconsistent shape must stay rejected."""
    if any(value is None for value in replacements.values()):
        return None
    result = deepcopy(raw)
    for marker in result.get('operation_markers', []):
        if marker.get('slot_name') in replacements:
            marker['slot_name'] = replacements[marker['slot_name']]
    slots = result.get('explicit_slot_mentions', {})
    renamed = {}
    for name, mentions in slots.items():
        key = replacements.get(name, name)
        if key in renamed:
            return None  # Never silently merge/delete current evidence.
        renamed[key] = mentions
    if 'explicit_slot_mentions' in result:
        result['explicit_slot_mentions'] = renamed
    return result
