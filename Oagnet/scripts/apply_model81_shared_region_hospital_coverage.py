"""Publish dealer-grain hospital coverage with a shared regional denominator.

Business owner decision (2026-09-07): when the metric is grouped by dealer,
the numerator is dealer-specific while the denominator is the complete
hospital population in the selected province/city.  Dealer constraints must
never reduce that denominator.

The migration is dry-run by default and is safe to execute repeatedly.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pymysql.cursors import DictCursor


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mysql_tool import _get_connection  # noqa: E402


MODEL_ID = 81
DOMAIN_ID = 205
COVERAGE_CODE = "screening_area_hospital_coverage"
NUMERATOR_CODE = "cooperating_hospital_count"
DENOMINATOR_CODE = "total_hospital_count_by_region"
DEALER_DIMENSION_CODE = "dealer"
DESCRIPTION_MARKER = "[dealer_shared_region_denominator:v1]"

COVERAGE_FORMULA = (
    "screening_area_hospital_coverage="
    "cooperating_hospital_count / total_hospital_count_by_region"
)
OLD_DENOMINATOR_FORMULA = (
    "target_city_hospital_count=COUNT(DISTINCT hospital.hospital_id)"
)
DENOMINATOR_FORMULA = (
    "total_hospital_count_by_region=COUNT(DISTINCT hospital.hospital_id)"
)

DESCRIPTION_SUFFIXES = {
    COVERAGE_CODE: (
        f"{DESCRIPTION_MARKER} 按经销商分组时，分子为该经销商在筛选省市范围内"
        "产生有效销售订单的去重医院数；分母为相同省市范围内医院主数据的全部"
        "去重医院数，所有经销商共用同一分母；经销商条件不得下推到分母。"
    ),
    NUMERATOR_CODE: (
        f"{DESCRIPTION_MARKER} 按经销商分组时，只在该经销商的有效销售订单内"
        "去重医院；省市条件同时限定订单和医院所属区域。"
    ),
    DENOMINATOR_CODE: (
        f"{DESCRIPTION_MARKER} 作为区域医院覆盖率分母时，只受省份、城市等区域"
        "条件约束；按经销商分组时仍使用同一区域全部医院数，不接受经销商条件。"
    ),
}


def _json_list(value: object) -> list[str]:
    if isinstance(value, list):
        return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))
    raw = str(value or "").strip()
    if not raw:
        return []
    decoded = json.loads(raw)
    if not isinstance(decoded, list):
        raise RuntimeError("经销商维度的指标绑定不是JSON数组")
    return list(dict.fromkeys(
        str(item).strip() for item in decoded if str(item).strip()
    ))


def _append_once(current: object, addition: str, *, separator: str = "；") -> str:
    value = str(current or "").strip()
    if DESCRIPTION_MARKER in value:
        return value
    return separator.join(item for item in (value, addition) if item)


def migrate(*, apply: bool) -> dict:
    connection = _get_connection()
    try:
        connection.begin()
        with connection.cursor(DictCursor) as cursor:
            cursor.execute(
                """
                SELECT id, indicator_code, business_desc, applicable_scenarios,
                       dependence_atomic_indicator, calculation_formula
                FROM semantic_model_indicator
                WHERE semantic_model_id=%s AND business_domain_id=%s
                  AND indicator_code IN (%s,%s,%s)
                  AND COALESCE(is_deleted,0)=0
                FOR UPDATE
                """,
                (
                    MODEL_ID,
                    DOMAIN_ID,
                    COVERAGE_CODE,
                    NUMERATOR_CODE,
                    DENOMINATOR_CODE,
                ),
            )
            rows = cursor.fetchall()
            by_code = {str(row["indicator_code"]): row for row in rows}
            expected_codes = {
                COVERAGE_CODE, NUMERATOR_CODE, DENOMINATOR_CODE,
            }
            if set(by_code) != expected_codes:
                raise RuntimeError("覆盖率及其分子、分母指标不存在或不唯一")
            if by_code[COVERAGE_CODE]["calculation_formula"] != COVERAGE_FORMULA:
                raise RuntimeError("区域医院覆盖率公式与已确认基线不一致")
            dependencies = {
                value.strip()
                for value in str(
                    by_code[COVERAGE_CODE]["dependence_atomic_indicator"] or ""
                ).split(",")
                if value.strip()
            }
            if dependencies != {NUMERATOR_CODE, DENOMINATOR_CODE}:
                raise RuntimeError("区域医院覆盖率依赖指标与已确认基线不一致")
            current_denominator_formula = str(
                by_code[DENOMINATOR_CODE]["calculation_formula"] or ""
            )
            if current_denominator_formula not in {
                OLD_DENOMINATOR_FORMULA, DENOMINATOR_FORMULA,
            }:
                raise RuntimeError("区域医院总数公式与预期不一致")

            cursor.execute(
                """
                SELECT id, indicator
                FROM semantic_model_dimension
                WHERE semantic_model_id=%s AND dim_code=%s
                  AND COALESCE(is_deleted,0)=0
                FOR UPDATE
                """,
                (MODEL_ID, DEALER_DIMENSION_CODE),
            )
            dimensions = cursor.fetchall()
            if len(dimensions) != 1:
                raise RuntimeError("经销商维度不存在或不唯一")
            dimension = dimensions[0]
            current_bindings = _json_list(dimension.get("indicator"))
            desired_bindings = list(current_bindings)
            for code in (NUMERATOR_CODE, COVERAGE_CODE):
                if code not in desired_bindings:
                    desired_bindings.append(code)
            if DENOMINATOR_CODE in desired_bindings:
                raise RuntimeError("分母指标不得绑定经销商维度")

            descriptions = {
                code: _append_once(row.get("business_desc"), DESCRIPTION_SUFFIXES[code])
                for code, row in by_code.items()
            }
            coverage_scenario = "按经销商统计区域医院覆盖率（区域公共分母）"
            scenarios = str(
                by_code[COVERAGE_CODE].get("applicable_scenarios") or ""
            ).strip()
            desired_scenarios = scenarios
            if coverage_scenario not in scenarios:
                desired_scenarios = ",".join(filter(None, (
                    scenarios, coverage_scenario,
                )))

            changes = {
                "dealer_metric_bindings": desired_bindings != current_bindings,
                "coverage_description": descriptions[COVERAGE_CODE]
                != str(by_code[COVERAGE_CODE].get("business_desc") or "").strip(),
                "numerator_description": descriptions[NUMERATOR_CODE]
                != str(by_code[NUMERATOR_CODE].get("business_desc") or "").strip(),
                "denominator_description": descriptions[DENOMINATOR_CODE]
                != str(by_code[DENOMINATOR_CODE].get("business_desc") or "").strip(),
                "coverage_scenario": desired_scenarios != scenarios,
                "denominator_formula_identifier": (
                    current_denominator_formula != DENOMINATOR_FORMULA
                ),
            }

            if apply:
                for code, description in descriptions.items():
                    cursor.execute(
                        """
                        UPDATE semantic_model_indicator
                        SET business_desc=%s, update_time=CURRENT_TIMESTAMP
                        WHERE id=%s
                        """,
                        (description, by_code[code]["id"]),
                    )
                cursor.execute(
                    """
                    UPDATE semantic_model_indicator
                    SET applicable_scenarios=%s, update_time=CURRENT_TIMESTAMP
                    WHERE id=%s
                    """,
                    (desired_scenarios, by_code[COVERAGE_CODE]["id"]),
                )
                cursor.execute(
                    """
                    UPDATE semantic_model_indicator
                    SET calculation_formula=%s, update_time=CURRENT_TIMESTAMP
                    WHERE id=%s
                    """,
                    (DENOMINATOR_FORMULA, by_code[DENOMINATOR_CODE]["id"]),
                )
                cursor.execute(
                    """
                    UPDATE semantic_model_dimension
                    SET indicator=%s, update_time=CURRENT_TIMESTAMP
                    WHERE id=%s
                    """,
                    (
                        json.dumps(desired_bindings, ensure_ascii=False),
                        dimension["id"],
                    ),
                )
                connection.commit()
            else:
                connection.rollback()

            return {
                "mode": "APPLY" if apply else "DRY_RUN",
                "semantic_model_id": MODEL_ID,
                "business_domain_id": DOMAIN_ID,
                "metric_code": COVERAGE_CODE,
                "grain": DEALER_DIMENSION_CODE,
                "denominator_policy": "SHARED_WITHIN_SELECTED_REGION",
                "dealer_metric_bindings_before": current_bindings,
                "dealer_metric_bindings_after": desired_bindings,
                "changes": changes,
                "requires_vector_reindex": any(changes.values()),
            }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(migrate(apply=args.apply), ensure_ascii=False))


if __name__ == "__main__":
    main()
