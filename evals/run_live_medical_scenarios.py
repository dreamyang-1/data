from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx


CASES_PATH = Path(__file__).with_name("medical_scenario_chain.json")
TERMINAL_STATUSES = {"COMPLETED"}


def _non_negative_int(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value >= 0


def _validate_concepts(case_id: str, field: str, value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, dict) or not value:
        raise ValueError(f"{case_id}: {field} must be a non-empty object")
    for name, alternatives in value.items():
        if (
            not isinstance(name, str)
            or not name.strip()
            or not isinstance(alternatives, list)
            or not alternatives
            or any(not isinstance(item, str) or not item.strip() for item in alternatives)
        ):
            raise ValueError(
                f"{case_id}: {field} values must be non-empty string lists"
            )


def _validate_analysis_contract(case_id: str, value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        raise ValueError(f"{case_id}: analysis_contract must be an object")
    allowed = {
        "type",
        "direction",
        "mode",
        "exact_count",
        "minimum_count",
        "maximum_count",
        "require_shortfall_disclosure",
        "minimum_profile_columns",
        "minimum_metric_columns",
    }
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(
            f"{case_id}: unsupported analysis_contract fields: {sorted(unknown)}"
        )
    if value.get("type") not in {"ranking", "explicit_object_comparison"}:
        raise ValueError(f"{case_id}: unsupported analysis_contract type")
    if value.get("direction") not in {None, "ascending", "descending"}:
        raise ValueError(f"{case_id}: invalid analysis_contract direction")
    if value.get("mode") not in {None, "BOUNDED_TOP_N", "FULL_ORDERED_SET"}:
        raise ValueError(f"{case_id}: invalid analysis_contract mode")
    for field in (
        "exact_count",
        "minimum_count",
        "maximum_count",
        "minimum_profile_columns",
        "minimum_metric_columns",
    ):
        if field in value and not _non_negative_int(value[field]):
            raise ValueError(
                f"{case_id}: analysis_contract.{field} must be a non-negative integer"
            )
    if not isinstance(value.get("require_shortfall_disclosure", False), bool):
        raise ValueError(
            f"{case_id}: analysis_contract.require_shortfall_disclosure must be a boolean"
        )


def _validate_report_contract(case_id: str, value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        raise ValueError(f"{case_id}: report_contract must be an object")
    allowed = {
        "section_count",
        "require_all_completed",
        "require_distinct_datasets",
        "require_distinct_query_fingerprints",
        "require_file_dataset_coverage",
    }
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(
            f"{case_id}: unsupported report_contract fields: {sorted(unknown)}"
        )
    if not _non_negative_int(value.get("section_count")) or value["section_count"] < 1:
        raise ValueError(f"{case_id}: report_contract.section_count must be positive")
    for field in allowed - {"section_count"}:
        if field in value and not isinstance(value[field], bool):
            raise ValueError(f"{case_id}: report_contract.{field} must be a boolean")


def _validate_web_contract(case_id: str, value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        raise ValueError(f"{case_id}: web_contract must be an object")
    allowed = {
        "minimum_records",
        "require_record_urls",
        "require_ranking_label_coverage",
    }
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(
            f"{case_id}: unsupported web_contract fields: {sorted(unknown)}"
        )
    if not _non_negative_int(value.get("minimum_records")):
        raise ValueError(f"{case_id}: web_contract.minimum_records is required")
    for field in allowed - {"minimum_records"}:
        if field in value and not isinstance(value[field], bool):
            raise ValueError(f"{case_id}: web_contract.{field} must be a boolean")


def validate_cases(cases: Any) -> list[dict[str, Any]]:
    if not isinstance(cases, list) or len(cases) != 19:
        raise ValueError("medical scenario benchmark must contain exactly 19 cases")
    ids = [case.get("id") for case in cases if isinstance(case, dict)]
    if len(ids) != len(cases) or len(ids) != len(set(ids)):
        raise ValueError("medical scenario ids must be present and unique")
    for case in cases:
        if not isinstance(case.get("question"), str) or not case["question"].strip():
            raise ValueError(f"{case['id']}: question is required")
        if not case.get("expected_intents"):
            raise ValueError(f"{case['id']}: expected_intents is required")
        clarification = case.get("clarification")
        if clarification not in {None, "optional", "required"}:
            raise ValueError(f"{case['id']}: invalid clarification mode")
        if clarification == "required" and not case.get("follow_up"):
            raise ValueError(f"{case['id']}: required clarification needs follow_up")
        minimum_rows = case.get("minimum_query_rows")
        if minimum_rows is not None and (
            isinstance(minimum_rows, bool)
            or not isinstance(minimum_rows, int)
            or minimum_rows < 0
        ):
            raise ValueError(f"{case['id']}: minimum_query_rows must be a non-negative integer")
        if not isinstance(case.get("require_source_watermark", False), bool):
            raise ValueError(f"{case['id']}: require_source_watermark must be a boolean")
        expected_granularity = case.get("expected_analysis_granularity")
        if expected_granularity is not None and expected_granularity not in {
            "day",
            "week",
            "month",
            "quarter",
            "year",
        }:
            raise ValueError(
                f"{case['id']}: expected_analysis_granularity is invalid"
            )
        _validate_concepts(
            case["id"],
            "required_query_column_concepts",
            case.get("required_query_column_concepts"),
        )
        _validate_concepts(
            case["id"],
            "non_null_preview_column_concepts",
            case.get("non_null_preview_column_concepts"),
        )
        _validate_analysis_contract(case["id"], case.get("analysis_contract"))
        _validate_report_contract(case["id"], case.get("report_contract"))
        _validate_web_contract(case["id"], case.get("web_contract"))
        for field in (
            "minimum_query_evidence",
            "minimum_each_query_rows",
            "minimum_files",
        ):
            value = case.get(field)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
            ):
                raise ValueError(f"{case['id']}: {field} must be a non-negative integer")
    return cases


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def request_body(case: dict[str, Any], conversation_id: str, question: str) -> dict[str, Any]:
    return {
        "application_id": "medical-scenario-eval",
        "conversation_id": conversation_id,
        "message_id": str(uuid4()),
        "question": question,
        "semantic_model_id": 81,
        "business_domain_ids": [],
        "history": [],
        "web_search": bool(case.get("web_search", False)),
    }


def _headers() -> dict[str, str]:
    return {
        "X-Tenant-Id": "medical-eval-tenant",
        "X-User-Id": "medical-eval-user",
        "X-Application-Id": "medical-scenario-eval",
    }


def call_sync(
    client: httpx.Client,
    endpoint: str,
    case: dict[str, Any],
    conversation_id: str,
    question: str,
) -> tuple[dict[str, Any], float, int]:
    started = time.perf_counter()
    response = client.post(
        endpoint,
        json=request_body(case, conversation_id, question),
        headers=_headers(),
    )
    latency = time.perf_counter() - started
    try:
        payload = response.json()
    except ValueError:
        payload = {"detail": response.text[:1000]}
    return payload, latency, response.status_code


def call_stream(
    client: httpx.Client,
    endpoint: str,
    case: dict[str, Any],
    conversation_id: str,
    question: str,
    *,
    deadline_seconds: float | None = None,
) -> tuple[dict[str, Any], float, int]:
    """Call the production SSE route and return its authoritative complete event."""

    started = time.perf_counter()
    events: list[dict[str, Any]] = []
    malformed_events: list[str] = []
    with client.stream(
        "POST",
        endpoint,
        json=request_body(case, conversation_id, question),
        headers={**_headers(), "Accept": "text/event-stream"},
    ) as response:
        if response.status_code != 200:
            body = response.read()
            latency = time.perf_counter() - started
            try:
                payload = json.loads(body)
            except (TypeError, ValueError):
                payload = {"detail": body.decode("utf-8", errors="replace")[:1000]}
            return payload, latency, response.status_code

        content_type = response.headers.get("content-type", "").lower()
        if "text/event-stream" not in content_type:
            malformed_events.append(f"unexpected content-type: {content_type}")

        data_lines: list[str] = []

        def consume_event() -> None:
            if not data_lines:
                return
            raw = "\n".join(data_lines)
            try:
                event = json.loads(raw)
            except ValueError:
                malformed_events.append(raw[:500])
            else:
                if isinstance(event, dict):
                    events.append(event)
                else:
                    malformed_events.append(raw[:500])
            data_lines.clear()

        for line in response.iter_lines():
            if (
                deadline_seconds is not None
                and time.perf_counter() - started > deadline_seconds
            ):
                raise httpx.TimeoutException(
                    f"SSE request exceeded {deadline_seconds} seconds",
                    request=response.request,
                )
            if line == "":
                consume_event()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip(" "))
            elif line.startswith(":"):
                continue
        consume_event()
        status_code = response.status_code

    latency = time.perf_counter() - started
    complete_events = [event for event in events if event.get("type") == "complete"]
    error_events = [event for event in events if event.get("type") == "error"]
    failures: list[str] = []
    if malformed_events:
        failures.append(f"MALFORMED_SSE_EVENT_{len(malformed_events)}")
    if error_events:
        failures.append(f"SSE_ERROR_{error_events[-1].get('code', 'UNKNOWN')}")
    if len(complete_events) != 1:
        failures.append(f"COMPLETE_EVENT_COUNT_{len(complete_events)}")

    payload = dict(complete_events[-1]) if complete_events else {
        "status": "STREAM_ERROR",
        "answer": "",
    }
    event_types = [str(event.get("type") or "") for event in events]
    if events and event_types[0] != "updata_state":
        failures.append("SSE_FIRST_EVENT_NOT_UPDATA_STATE")
    if complete_events and event_types[-1] != "complete":
        failures.append("SSE_COMPLETE_NOT_LAST")

    output_chunks = [
        str(event.get("content") or "")
        for event in events
        if event.get("type") == "message_chunk" and event.get("step") == "output"
    ]
    answer_events = [event for event in events if event.get("type") == "answer"]
    completed_answer = str(payload.get("answer") or "")
    if completed_answer:
        if len(answer_events) != 1:
            failures.append(f"ANSWER_EVENT_COUNT_{len(answer_events)}")
        elif str(answer_events[0].get("content") or "") != completed_answer:
            failures.append("ANSWER_EVENT_MISMATCH")
        if "".join(output_chunks) != completed_answer:
            failures.append("MESSAGE_CHUNKS_MISMATCH")

    payload["_transport_failures"] = failures
    payload["_event_types"] = event_types
    return payload, latency, status_code


def call(
    client: httpx.Client,
    endpoint: str,
    case: dict[str, Any],
    conversation_id: str,
    question: str,
    *,
    transport: str,
    deadline_seconds: float | None = None,
) -> tuple[dict[str, Any], float, int]:
    if transport == "stream":
        return call_stream(
            client,
            endpoint,
            case,
            conversation_id,
            question,
            deadline_seconds=deadline_seconds,
        )
    return call_sync(client, endpoint, case, conversation_id, question)


def _normalized_identifier(value: Any) -> str:
    return re.sub(r"[\W_]+", "", str(value or "").casefold())


def _matches_concept(value: str, alternatives: list[str]) -> bool:
    normalized = _normalized_identifier(value)
    for raw_alternative in alternatives:
        exact = raw_alternative.startswith("=")
        alternative = _normalized_identifier(
            raw_alternative[1:] if exact else raw_alternative
        )
        if alternative and (
            normalized == alternative if exact else alternative in normalized
        ):
            return True
    return False


def _query_evidence(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        item
        for item in payload.get("evidence", [])
        if isinstance(item, dict) and item.get("kind") == "QUERY_RESULT"
    ]


def _query_columns(query_evidence: list[dict[str, Any]]) -> list[str]:
    return list(dict.fromkeys(
        column
        for item in query_evidence
        for column in (
            item.get("payload", {}).get("columns", [])
            if isinstance(item.get("payload"), dict)
            else []
        )
        if isinstance(column, str) and column.strip()
    ))


def _analysis_payloads(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        item["payload"]
        for item in payload.get("evidence", [])
        if isinstance(item, dict)
        and item.get("kind") == "ANALYSIS_RESULT"
        and isinstance(item.get("payload"), dict)
    ]


def _is_number(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def _assess_ranking_contract(
    contract: dict[str, Any],
    analysis_payloads: list[dict[str, Any]],
    query_columns: list[str],
) -> tuple[list[str], list[str]]:
    failures: list[str] = []
    candidates = [
        item.get("facts")
        for item in analysis_payloads
        if isinstance(item.get("facts"), dict)
        and isinstance(item["facts"].get("rankings"), list)
    ]
    if not candidates:
        return ["ANALYSIS_RANKING_CONTRACT_MISSING"], []
    facts = candidates[0]
    rankings = facts["rankings"]
    metric_column = facts.get("metric_column")
    label_column = facts.get("label_column")
    if (
        not isinstance(metric_column, str)
        or not metric_column.strip()
        or metric_column not in query_columns
    ):
        failures.append("RANKING_METRIC_COLUMN_INVALID")
    if (
        not isinstance(label_column, str)
        or not label_column.strip()
        or label_column not in query_columns
        or label_column == metric_column
    ):
        failures.append("RANKING_LABEL_COLUMN_INVALID")
    if not rankings:
        failures.append("RANKING_ROWS_EMPTY")
        return failures, []

    exact_count = contract.get("exact_count")
    if exact_count is not None and len(rankings) != exact_count:
        failures.append(f"RANKING_COUNT_EXPECTED_{exact_count}_ACTUAL_{len(rankings)}")
    minimum_count = contract.get("minimum_count")
    if minimum_count is not None and len(rankings) < minimum_count:
        failures.append(f"RANKING_COUNT_BELOW_{minimum_count}")
    maximum_count = contract.get("maximum_count")
    if maximum_count is not None and len(rankings) > maximum_count:
        failures.append(f"RANKING_COUNT_ABOVE_{maximum_count}")
    if (
        contract.get("require_shortfall_disclosure")
        and maximum_count is not None
        and len(rankings) < maximum_count
    ):
        if facts.get("requested_object_count") != maximum_count:
            failures.append("RANKING_REQUESTED_COUNT_MISMATCH")
        if facts.get("ranking_shortfall") != maximum_count - len(rankings):
            failures.append("RANKING_SHORTFALL_INVALID")
    if facts.get("returned_object_count") != len(rankings):
        failures.append("RANKING_RETURNED_COUNT_MISMATCH")
    mode = contract.get("mode")
    if mode and facts.get("ranking_mode") != mode:
        failures.append(f"RANKING_MODE_EXPECTED_{mode}")

    labels: list[str] = []
    values: list[float] = []
    for index, item in enumerate(rankings, start=1):
        if not isinstance(item, dict) or item.get("rank") != index:
            failures.append("RANKING_SEQUENCE_INVALID")
            break
        label = str(item.get("label") or "").strip()
        if not label:
            failures.append("RANKING_LABEL_EMPTY")
            break
        if not _is_number(item.get("value")):
            failures.append("RANKING_VALUE_NOT_NUMERIC")
            break
        labels.append(label)
        values.append(float(item["value"]))
    if labels and len(labels) != len(set(labels)):
        failures.append("RANKING_LABELS_NOT_UNIQUE")

    direction = contract.get("direction")
    expected_descending = direction == "descending"
    if direction and facts.get("descending") is not expected_descending:
        failures.append(f"RANKING_DIRECTION_EXPECTED_{direction}")
    if len(values) == len(rankings):
        expected_values = sorted(values, reverse=expected_descending)
        if direction and values != expected_values:
            failures.append("RANKING_VALUES_NOT_SORTED")

    minimum_profiles = contract.get("minimum_profile_columns", 0)
    profile_columns = facts.get("profile_columns")
    if minimum_profiles and (
        not isinstance(profile_columns, list)
        or len({str(item) for item in profile_columns if str(item).strip()})
        < minimum_profiles
    ):
        failures.append(f"RANKING_PROFILE_COLUMNS_BELOW_{minimum_profiles}")
    elif minimum_profiles:
        expected_profile_columns = set(profile_columns)
        if any(
            not isinstance(item.get("profile"), dict)
            or not expected_profile_columns.issubset(item["profile"])
            for item in rankings
            if isinstance(item, dict)
        ):
            failures.append("RANKING_PROFILE_VALUES_INCOMPLETE")
    return failures, labels


def _assess_object_comparison_contract(
    contract: dict[str, Any], analysis_payloads: list[dict[str, Any]]
) -> list[str]:
    candidates = [
        item.get("facts")
        for item in analysis_payloads
        if isinstance(item.get("facts"), dict)
        and item["facts"].get("comparison_mode") == "DESCRIPTIVE_NO_BASELINE"
    ]
    if not candidates:
        return ["ANALYSIS_OBJECT_COMPARISON_CONTRACT_MISSING"]
    facts = candidates[0]
    failures: list[str] = []
    objects = facts.get("objects")
    metric_columns = facts.get("metric_columns")
    exact_count = contract.get("exact_count")
    if not isinstance(objects, list) or (
        exact_count is not None and len(objects) != exact_count
    ):
        actual = len(objects) if isinstance(objects, list) else "invalid"
        failures.append(f"COMPARISON_COUNT_EXPECTED_{exact_count}_ACTUAL_{actual}")
    minimum_metrics = contract.get("minimum_metric_columns", 0)
    if not isinstance(metric_columns, list) or len(set(metric_columns)) < minimum_metrics:
        failures.append(f"COMPARISON_METRIC_COLUMNS_BELOW_{minimum_metrics}")
    if isinstance(objects, list) and isinstance(metric_columns, list):
        labels = [
            str(item.get("label") or "").strip()
            for item in objects
            if isinstance(item, dict)
        ]
        if len(labels) != len(objects) or any(not label for label in labels):
            failures.append("COMPARISON_LABELS_INVALID")
        elif len(labels) != len(set(labels)):
            failures.append("COMPARISON_LABELS_NOT_UNIQUE")
        if any(
            not isinstance(item.get("metrics"), dict)
            or any(not _is_number(item["metrics"].get(metric)) for metric in metric_columns)
            for item in objects
            if isinstance(item, dict)
        ):
            failures.append("COMPARISON_METRIC_VALUES_INCOMPLETE")
    return failures


def _assess_report_contract(
    contract: dict[str, Any], payload: dict[str, Any]
) -> list[str]:
    manifests = [
        item.get("payload")
        for item in payload.get("evidence", [])
        if isinstance(item, dict)
        and item.get("kind") == "REPORT_DATASET_MANIFEST"
        and isinstance(item.get("payload"), dict)
    ]
    if len(manifests) != 1:
        return [f"REPORT_MANIFEST_COUNT_{len(manifests)}"]
    manifest = manifests[0]
    expected_count = contract["section_count"]
    sections = manifest.get("sections")
    failures: list[str] = []
    if (
        manifest.get("section_count") != expected_count
        or not isinstance(sections, list)
        or len(sections) != expected_count
    ):
        failures.append(f"REPORT_SECTION_COUNT_EXPECTED_{expected_count}")
        return failures

    task_ids = [
        str(section.get("task_id") or "").strip()
        for section in sections
        if isinstance(section, dict)
    ]
    if (
        len(task_ids) != expected_count
        or any(not task_id for task_id in task_ids)
        or len(task_ids) != len(set(task_ids))
    ):
        failures.append("REPORT_SECTION_TASK_IDS_INVALID")
    if contract.get("require_all_completed"):
        if (
            manifest.get("available_section_count") != expected_count
            or manifest.get("fully_completed_section_count") != expected_count
            or any(
                not isinstance(section, dict) or section.get("status") != "COMPLETED"
                for section in sections
            )
        ):
            failures.append("REPORT_SECTIONS_NOT_FULLY_COMPLETED")

    dataset_ids = [
        str(section.get("dataset_id") or "").strip()
        for section in sections
        if isinstance(section, dict)
    ]
    if contract.get("require_distinct_datasets") and (
        len(dataset_ids) != expected_count
        or any(not dataset_id for dataset_id in dataset_ids)
        or len(set(dataset_ids)) != expected_count
        or set(payload.get("dataset_ids") or []) != set(dataset_ids)
    ):
        failures.append("REPORT_DATASETS_NOT_DISTINCT")

    fingerprints: list[str] = []
    for section in sections:
        proofs = section.get("query_proofs") if isinstance(section, dict) else None
        if not isinstance(proofs, list) or not proofs:
            failures.append("REPORT_SECTION_QUERY_PROOF_MISSING")
            continue
        for proof in proofs:
            if not isinstance(proof, dict):
                failures.append("REPORT_SECTION_QUERY_PROOF_INVALID")
                continue
            fingerprint = str(proof.get("result_fingerprint") or "").strip()
            columns = proof.get("columns")
            if (
                str(proof.get("quality_status") or "").upper() != "PASS"
                or not fingerprint
                or not proof.get("data_as_of")
                or not isinstance(columns, list)
                or not columns
            ):
                failures.append("REPORT_SECTION_QUERY_PROOF_INVALID")
            if fingerprint:
                fingerprints.append(fingerprint)
    if contract.get("require_distinct_query_fingerprints") and (
        len(fingerprints) < expected_count
        or len(set(fingerprints)) != len(fingerprints)
    ):
        failures.append("REPORT_QUERY_FINGERPRINTS_NOT_DISTINCT")

    if contract.get("require_file_dataset_coverage"):
        covered_dataset_ids = {
            str(dataset_id)
            for item in payload.get("files", [])
            if isinstance(item, dict)
            for dataset_id in (
                list(item.get("dataset_ids") or [])
                + ([item["dataset_id"]] if item.get("dataset_id") else [])
            )
        }
        if not set(dataset_ids).issubset(covered_dataset_ids):
            failures.append("REPORT_FILE_DATASET_COVERAGE_INCOMPLETE")
    return list(dict.fromkeys(failures))


def _web_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for item in payload.get("evidence", []):
        if (
            isinstance(item, dict)
            and item.get("kind") == "WEB_SEARCH_RESULT"
            and isinstance(item.get("payload"), dict)
        ):
            records.extend(
                record
                for record in item["payload"].get("records", [])
                if isinstance(record, dict)
            )
    return records


def _assess_web_contract(
    contract: dict[str, Any],
    payload: dict[str, Any],
    ranking_labels: list[str],
) -> list[str]:
    records = _web_records(payload)
    unique_records = {
        (
            str(item.get("url") or "").strip(),
            str(item.get("title") or "").strip(),
            str(item.get("snippet") or item.get("summary") or "").strip(),
        )
        for item in records
    }
    minimum_records = contract["minimum_records"]
    failures: list[str] = []
    if len(unique_records) < minimum_records:
        failures.append(f"WEB_RECORDS_BELOW_{minimum_records}")
    if contract.get("require_record_urls") and any(
        not str(item.get("url") or "").strip().lower().startswith(("http://", "https://"))
        for item in records
    ):
        failures.append("WEB_RECORD_URL_MISSING")
    if contract.get("require_record_urls") and not records:
        failures.append("WEB_RECORD_URL_MISSING")
    if contract.get("require_ranking_label_coverage"):
        record_text = _normalized_identifier(" ".join(
            str(value)
            for item in records
            for value in item.values()
            if isinstance(value, (str, int, float))
        ))
        missing_labels = [
            label for label in ranking_labels
            if _normalized_identifier(label) not in record_text
        ]
        if not ranking_labels or missing_labels:
            failures.append(
                f"WEB_RANKING_LABEL_COVERAGE_{len(ranking_labels) - len(missing_labels)}_OF_{len(ranking_labels)}"
            )
    return failures


def _json_objects_in_text(text: str) -> list[dict[str, Any]]:
    decoder = json.JSONDecoder()
    objects: list[dict[str, Any]] = []

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            objects.append(value)
            for nested in value.values():
                if isinstance(nested, (dict, list)):
                    collect(nested)
        elif isinstance(value, list):
            for nested in value:
                collect(nested)

    position = 0
    while position < len(text):
        match = re.search(r"[\[{]", text[position:])
        if match is None:
            break
        start = position + match.start()
        try:
            value, consumed = decoder.raw_decode(text[start:])
        except ValueError:
            position = start + 1
            continue
        collect(value)
        position = start + consumed
    return objects


def _assess_non_null_previews(
    concepts: dict[str, list[str]], answer: str
) -> list[str]:
    rows = _json_objects_in_text(answer)
    failures: list[str] = []
    for name, alternatives in concepts.items():
        values = [
            value
            for row in rows
            for column, value in row.items()
            if _matches_concept(str(column), alternatives)
        ]
        if not values:
            failures.append(f"GROUP_PREVIEW_MISSING_{name}")
            continue
        if any(
            value is None
            or (isinstance(value, str) and value.strip().casefold() in {
                "", "null", "none", "nan", "n/a",
            })
            for value in values
        ):
            failures.append(f"GROUP_PREVIEW_NULL_{name}")
    return failures


def assess(
    case: dict[str, Any],
    payload: dict[str, Any],
    status_code: int,
    conversation_id: str,
) -> tuple[bool, list[str]]:
    failures: list[str] = []
    if status_code != 200:
        failures.append(f"HTTP_{status_code}")
        return False, failures
    status = payload.get("status")
    if status not in TERMINAL_STATUSES:
        failures.append(f"NON_TERMINAL_{status}")
    if payload.get("conversation_id") != conversation_id:
        failures.append("CONVERSATION_ID_MISMATCH")
    failures.extend(str(item) for item in payload.get("_transport_failures", []))
    if payload.get("intent") not in case["expected_intents"]:
        failures.append(f"INTENT_{payload.get('intent')}")
    evidence_kinds = {
        item.get("kind")
        for item in payload.get("evidence", [])
        if isinstance(item, dict)
    }
    for kind in case.get("required_evidence", []):
        if kind not in evidence_kinds:
            failures.append(f"MISSING_EVIDENCE_{kind}")
    query_evidence = _query_evidence(payload)
    query_columns = _query_columns(query_evidence)
    if "QUERY_RESULT" in case.get("required_evidence", []):
        for index, item in enumerate(query_evidence):
            proof = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            if str(proof.get("quality_status") or "").upper() != "PASS":
                failures.append(f"QUERY_QUALITY_NOT_PASS_{index}")
            if not proof.get("result_fingerprint") or not proof.get("data_as_of"):
                failures.append(f"QUERY_PROVENANCE_INCOMPLETE_{index}")
    for name, alternatives in case.get(
        "required_query_column_concepts", {}
    ).items():
        if not any(
            _matches_concept(column, alternatives) for column in query_columns
        ):
            failures.append(f"QUERY_COLUMN_CONCEPT_MISSING_{name}")
    if case.get("require_source_watermark"):
        source_watermark_proofs = [
            item.get("payload")
            for item in query_evidence
            if isinstance(item.get("payload"), dict)
            and item["payload"].get("source_data_as_of")
            and item["payload"].get("source_watermark_field")
        ]
        if not source_watermark_proofs:
            failures.append("MISSING_QUERY_SOURCE_WATERMARK")
    expected_granularity = case.get("expected_analysis_granularity")
    analysis_payloads = _analysis_payloads(payload)
    if expected_granularity:
        analysis_facts = [
            item.get("facts", {})
            for item in analysis_payloads
            if isinstance(item.get("facts"), dict)
        ]
        actual_granularities = {
            str(facts.get("actual_granularity") or "").strip().lower()
            for facts in analysis_facts
            if facts.get("actual_granularity")
        }
        if expected_granularity not in actual_granularities:
            actual = ",".join(sorted(actual_granularities)) or "missing"
            failures.append(
                f"ANALYSIS_GRANULARITY_EXPECTED_{expected_granularity}_ACTUAL_{actual}"
            )
    ranking_labels: list[str] = []
    analysis_contract = case.get("analysis_contract")
    if analysis_contract:
        if analysis_contract["type"] == "ranking":
            analysis_failures, ranking_labels = _assess_ranking_contract(
                analysis_contract, analysis_payloads, query_columns
            )
            failures.extend(analysis_failures)
        else:
            failures.extend(
                _assess_object_comparison_contract(
                    analysis_contract, analysis_payloads
                )
            )
    minimum_query_rows = case.get("minimum_query_rows")
    if minimum_query_rows is not None:
        row_counts = [
            item.get("payload", {}).get("row_count")
            for item in payload.get("evidence", [])
            if isinstance(item, dict) and item.get("kind") == "QUERY_RESULT"
        ]
        numeric_rows = [value for value in row_counts if isinstance(value, int)]
        if not numeric_rows or max(numeric_rows) < int(minimum_query_rows):
            failures.append(f"QUERY_ROWS_BELOW_{minimum_query_rows}")
    minimum_query_evidence = case.get("minimum_query_evidence")
    if (
        minimum_query_evidence is not None
        and len(query_evidence) < int(minimum_query_evidence)
    ):
        failures.append(f"QUERY_EVIDENCE_BELOW_{minimum_query_evidence}")
    minimum_each_query_rows = case.get("minimum_each_query_rows")
    if minimum_each_query_rows is not None:
        row_counts = [
            item.get("payload", {}).get("row_count") for item in query_evidence
        ]
        if (
            not row_counts
            or any(
                not isinstance(value, int)
                or value < int(minimum_each_query_rows)
                for value in row_counts
            )
        ):
            failures.append(f"QUERY_ROWS_EACH_BELOW_{minimum_each_query_rows}")
    minimum_files = case.get("minimum_files")
    if minimum_files is not None:
        files = payload.get("files")
        file_count = len(files) if isinstance(files, list) else 0
        if file_count < int(minimum_files):
            failures.append(f"FILES_BELOW_{minimum_files}")
    report_contract = case.get("report_contract")
    if report_contract:
        failures.extend(_assess_report_contract(report_contract, payload))
    web_contract = case.get("web_contract")
    if web_contract:
        failures.extend(
            _assess_web_contract(web_contract, payload, ranking_labels)
        )
    answer = str(payload.get("answer") or "")
    if not answer.strip():
        failures.append("EMPTY_ANSWER")
    if (
        isinstance(analysis_contract, dict)
        and analysis_contract.get("require_shortfall_disclosure")
        and analysis_contract.get("maximum_count") is not None
        and len(ranking_labels) < int(analysis_contract["maximum_count"])
        and not re.search(r"不足|仅有|只有|少于", answer)
    ):
        failures.append("RANKING_SHORTFALL_NOT_DISCLOSED")
    non_null_preview_concepts = case.get("non_null_preview_column_concepts")
    if non_null_preview_concepts:
        failures.extend(
            _assess_non_null_previews(non_null_preview_concepts, answer)
        )
    for expected in case.get("answer_contains", []):
        if expected not in answer:
            failures.append(f"ANSWER_MISSING_{expected}")
    return not failures, failures


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the 19 medical business scenarios against a live DataAgent")
    parser.add_argument("--base-url", default="http://127.0.0.1:8088")
    parser.add_argument("--timeout", type=float, default=150.0)
    parser.add_argument(
        "--transport",
        choices=("stream", "sync"),
        default="stream",
        help="stream validates the frontend SSE contract; sync tests /agent_chat",
    )
    parser.add_argument("--case", action="append", dest="case_ids")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")

    cases = validate_cases(json.loads(CASES_PATH.read_text(encoding="utf-8")))
    if args.case_ids:
        selected = set(args.case_ids)
        known = {case["id"] for case in cases}
        unknown = sorted(selected - known)
        if unknown:
            parser.error(f"unknown case ids: {', '.join(unknown)}")
        cases = [case for case in cases if case["id"] in selected]
    endpoint = args.base_url.rstrip("/") + (
        "/agent_chat/stream" if args.transport == "stream" else "/agent_chat"
    )
    results: list[dict[str, Any]] = []

    with httpx.Client(timeout=args.timeout) as client:
        for case in cases:
            conversation_id = f"medical-{case['id'].lower()}-{uuid4().hex[:10]}"
            turn_started = time.perf_counter()
            try:
                payload, latency, status_code = call(
                    client,
                    endpoint,
                    case,
                    conversation_id,
                    case["question"],
                    transport=args.transport,
                    deadline_seconds=args.timeout,
                )
            except httpx.RequestError as exc:
                latency = time.perf_counter() - turn_started
                status_code = 0
                payload = {
                    "status": "TRANSPORT_ERROR",
                    "answer": "",
                    "_transport_failures": [type(exc).__name__],
                }
            first_status = payload.get("status")
            total_latency = latency
            turns = 1
            clarification_mode = case.get("clarification")
            first_turn_valid = True
            first_turn_failures = [
                f"FIRST_TURN_{item}"
                for item in payload.get("_transport_failures", [])
            ]
            if status_code == 200 and payload.get("conversation_id") != conversation_id:
                first_turn_failures.append("FIRST_TURN_CONVERSATION_ID_MISMATCH")
            if clarification_mode == "required" and first_status != "NEEDS_CLARIFICATION":
                first_turn_valid = False
            if first_status == "NEEDS_CLARIFICATION" and not (
                payload.get("clarification_questions") or payload.get("clarification_items")
            ):
                first_turn_valid = False
            if first_status == "NEEDS_CLARIFICATION" and case.get("follow_up"):
                follow_started = time.perf_counter()
                try:
                    payload, follow_latency, status_code = call(
                        client,
                        endpoint,
                        case,
                        conversation_id,
                        case["follow_up"],
                        transport=args.transport,
                        deadline_seconds=args.timeout,
                    )
                except httpx.RequestError as exc:
                    follow_latency = time.perf_counter() - follow_started
                    status_code = 0
                    payload = {
                        "status": "TRANSPORT_ERROR",
                        "answer": "",
                        "_transport_failures": [type(exc).__name__],
                    }
                total_latency += follow_latency
                turns += 1
            elif first_status == "NEEDS_CLARIFICATION" and clarification_mode not in {"required", "optional"}:
                first_turn_valid = False
            ok, failures = assess(case, payload, status_code, conversation_id)
            if turns > 1:
                failures.extend(first_turn_failures)
            if not first_turn_valid:
                failures.append("CLARIFICATION_BEHAVIOR_INVALID")
            ok = not failures
            results.append({
                "id": case["id"],
                "category": case["category"],
                "ok": ok,
                "failures": failures,
                "turns": turns,
                "first_status": first_status,
                "final_status": payload.get("status"),
                "intent": payload.get("intent"),
                "latency_seconds": round(total_latency, 3),
                "reliability": (payload.get("reliability") or {}).get("level"),
                "answer_preview": str(payload.get("answer") or "")[:500],
                "evidence": [
                    {
                        "kind": item.get("kind"),
                        "row_count": item.get("payload", {}).get("row_count"),
                    }
                    for item in payload.get("evidence", [])
                    if isinstance(item, dict)
                ],
                "stream_event_types": payload.get("_event_types", []),
            })

    latencies = [item["latency_seconds"] for item in results]
    passed = sum(bool(item["ok"]) for item in results)
    output = {
        "summary": {
            "transport": args.transport,
            "history_mode": "server_session_recovery",
            "assessment_scope": (
                "strict chain-contract checks plus configured business ground-truth "
                "row assertions"
            ),
            "total": len(results),
            "passed": passed,
            "pass_rate": round(passed / len(results), 4) if results else 0.0,
            "average_seconds": round(statistics.fmean(latencies), 3) if latencies else 0.0,
            "p50_seconds": round(percentile(latencies, 0.5), 3),
            "p90_seconds": round(percentile(latencies, 0.9), 3),
            "max_seconds": round(max(latencies), 3) if latencies else 0.0,
        },
        "results": results,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
