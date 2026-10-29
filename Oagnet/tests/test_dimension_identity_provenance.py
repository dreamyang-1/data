import json
import unittest
from types import SimpleNamespace

from agent import _normalize_semantic_references


def _normalize(ast: dict, dimensions: list) -> dict:
    return json.loads(_normalize_semantic_references(
        json.dumps(ast),
        {"dimensions": [SimpleNamespace(metadata=m) for m in dimensions]},
        "统计近半年各个产线故障发生次数",
    ))


RECALLED_LINE_DIMENSION = {
    "dim_code": "line_or_equipment_path",
    "dim_name": "产线/设备路径",
    "synonyms": json.dumps(["产线", "各个产线"]),
    "bind_entities": json.dumps([
        {
            "attr": "device_group",
            "mappingTable": "eam_zt_repairorders",
            "mappingColumn": "DeviceGroupCode",
        }
    ]),
}


class DimensionIdentityProvenanceTests(unittest.TestCase):
    def test_unique_recalled_code_receives_canonical_alias(self):
        result = _normalize(
            {
                "subject": {"entity": "ent_repair_order"},
                "metrics": [{"name": "fault_count"}],
                "dimensions": [{
                    "name": "line_or_equipment_path",
                    "attr": None,
                    "level": None,
                    "granularity": None,
                }],
            },
            [RECALLED_LINE_DIMENSION],
        )
        dimension = result["dimensions"][0]
        self.assertEqual("line_or_equipment_path", dimension["name"])
        self.assertEqual("产线/设备路径", dimension["alias"])

    def test_model_fabricated_alias_is_overwritten_by_recalled_name(self):
        result = _normalize(
            {
                "subject": {"entity": "ent_repair_order"},
                "metrics": [{"name": "fault_count"}],
                "dimensions": [{
                    "name": "line_or_equipment_path",
                    "alias": "生产车间分组",
                    "attr": None,
                    "level": None,
                    "granularity": None,
                }],
            },
            [RECALLED_LINE_DIMENSION],
        )
        self.assertEqual("产线/设备路径", result["dimensions"][0]["alias"])

    def test_unrecalled_code_loses_fabricated_alias(self):
        result = _normalize(
            {
                "subject": {"entity": "ent_repair_order"},
                "metrics": [{"name": "fault_count"}],
                "dimensions": [{
                    "name": "line_or_equipment_path",
                    "alias": "伪造产线",
                    "attr": None,
                    "level": None,
                    "granularity": None,
                }],
            },
            [],
        )
        self.assertNotIn("alias", result["dimensions"][0])

    def test_duplicate_codes_do_not_receive_alias_and_fabricated_alias_removed(self):
        result = _normalize(
            {
                "subject": {"entity": "ent_repair_order"},
                "metrics": [{"name": "fault_count"}],
                "dimensions": [{
                    "name": "line_or_equipment_path",
                    "alias": "伪造产线",
                    "attr": None,
                    "level": None,
                    "granularity": None,
                }],
            },
            [
                RECALLED_LINE_DIMENSION,
                {**RECALLED_LINE_DIMENSION, "dim_name": "另一作用域产线路径"},
            ],
        )
        self.assertNotIn("alias", result["dimensions"][0])

    def test_code_without_canonical_name_receives_no_alias(self):
        result = _normalize(
            {
                "subject": {"entity": "ent_repair_order"},
                "metrics": [{"name": "fault_count"}],
                "dimensions": [{
                    "name": "line_or_equipment_path",
                    "alias": "伪造产线",
                    "attr": None,
                    "level": None,
                    "granularity": None,
                }],
            },
            [{**RECALLED_LINE_DIMENSION, "dim_name": ""}],
        )
        self.assertNotIn("alias", result["dimensions"][0])

    def test_physical_entity_field_is_not_treated_as_logical_dimension(self):
        result = _normalize(
            {
                "subject": {"entity": "ent_repair_order"},
                "metrics": [{"name": "fault_count"}],
                "dimensions": [{
                    "name": "eam_zt_repairorders.DeviceGroupCode",
                    "alias": "设备组编码",
                    "attr": None,
                    "level": None,
                    "granularity": None,
                }],
            },
            [RECALLED_LINE_DIMENSION],
        )
        dimension = result["dimensions"][0]
        self.assertEqual("eam_zt_repairorders.DeviceGroupCode", dimension["name"])
        self.assertNotIn("alias", dimension)


if __name__ == "__main__":
    unittest.main()
