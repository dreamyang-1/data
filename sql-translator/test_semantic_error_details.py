"""Error detail contract tests for semantic translation failures.

Positive cases: confirmed semantic validation failures expose a structured
semantic validation report naming the failing asset/layer.
Negative cases: unknown internal failures and transport failures stay
sanitized. The raw exception text, fabricated credentials, full SQL, and
internal addresses must never appear anywhere in the public response.
"""
import json
import unittest

import redis

from test_sql_translator_hardening import base_ast, translator

_PRIVATE_SENTINEL = "PRIVATE_SENTINEL"
_FABRICATED_CREDENTIAL = "password=hunter2secret"
_FABRICATED_SQL = "DROP TABLE users; DELETE FROM orders"
_FABRICATED_ADDRESS = "10.20.30.40:6379"
_SENTINELS = (
    _PRIVATE_SENTINEL,
    _FABRICATED_CREDENTIAL,
    _FABRICATED_SQL,
    _FABRICATED_ADDRESS,
)


def flatten_strings(value):
    """Yield every string contained anywhere in a response payload."""
    if isinstance(value, dict):
        for item in value.values():
            yield from flatten_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from flatten_strings(item)
    else:
        yield str(value)


class SemanticErrorDetailTests(unittest.TestCase):
    def test_execute_query_confirmed_semantic_failure_keeps_structured_report(self):
        ast = base_ast(metrics=[{"name": "invented", "alias": "编造指标"}])
        result = translator().execute_query(json.dumps(ast, ensure_ascii=False), "6")

        self.assertFalse(result["success"])
        self.assertIsNone(result["sql"])
        # Existing error code and retry semantics must stay unchanged.
        self.assertEqual("SQL_TRANSLATION_FAILED", result["error_code"])
        self.assertFalse(result["retryable"])

        report = result["semantic_validation_report"]
        self.assertEqual("FAIL", report["status"])
        self.assertEqual("SEMANTIC_ASSET_NOT_FOUND", report["errors"][0]["code"])
        self.assertEqual("SEMANTIC", report["errors"][0]["layer"])
        self.assertEqual("FAIL", report["layers"]["semantic"]["status"])
        self.assertEqual("NOT_RUN", report["layers"]["syntax"]["status"])
        self.assertEqual("NOT_RUN", report["layers"]["business"]["status"])
        # The controlled canonical identifier of the missing asset must stay
        # visible for repair, assembled only from the known error format.
        self.assertEqual("不存在指标: invented", report["errors"][0]["message"])
        self.assertIn("不存在指标: invented", result["error"])
        self.assertIn("不存在于当前语义模型", result["error"])

    def test_execute_query_confirmed_report_matches_translate_only_failure_shape(self):
        ast = base_ast(metrics=[{"name": "invented", "alias": "编造指标"}])
        asl = json.dumps(ast, ensure_ascii=False)
        executed = translator().execute_query(asl, "6")
        translated = translator().translate_only(asl, "6")

        self.assertFalse(translated["success"])
        self.assertEqual(
            translated["semantic_validation_report"],
            executed["semantic_validation_report"],
        )

    def test_redis_transport_failure_does_not_fabricate_semantic_details(self):
        value = translator()

        def unavailable(*_args, **_kwargs):
            raise redis.ConnectionError("secret redis endpoint")

        value.translate = unavailable
        result = value.execute_query(json.dumps(base_ast()), "6")

        self.assertEqual("SEMANTIC_DSL_UNAVAILABLE", result["error_code"])
        self.assertTrue(result["retryable"])
        self.assertNotIn("secret", result["error"])
        # Transport failures must not masquerade as semantic validation evidence.
        self.assertNotIn("semantic_validation_report", result)

    def test_unknown_runtime_failure_is_sanitized_without_semantic_report(self):
        value = translator()

        def explode(*_args, **_kwargs):
            raise RuntimeError(
                f"{_PRIVATE_SENTINEL} with {_FABRICATED_CREDENTIAL} at "
                f"{_FABRICATED_ADDRESS}; executed {_FABRICATED_SQL}"
            )

        value.translate = explode
        result = value.execute_query(json.dumps(base_ast()), "6")

        self.assertEqual("SQL_TRANSLATION_FAILED", result["error_code"])
        self.assertFalse(result["retryable"])
        # Unknown failures must not be presented as a confirmed semantic failure.
        self.assertNotIn("semantic_validation_report", result)
        self.assertIn("内部处理错误", result["error"])
        # The entire response must stay free of every private sentinel.
        flattened = "\n".join(flatten_strings(result))
        for sentinel in _SENTINELS:
            self.assertNotIn(sentinel, flattened)

    def test_unknown_value_error_matching_no_code_is_sanitized_too(self):
        # A ValueError whose wording matches no registered semantic code is not a
        # confirmed semantic failure and must not earn a semantic report.
        value = translator()

        def explode(*_args, **_kwargs):
            raise ValueError(
                f"unexpected internal state {_PRIVATE_SENTINEL} "
                f"credential {_FABRICATED_CREDENTIAL}"
            )

        value.translate = explode
        result = value.execute_query(json.dumps(base_ast()), "6")

        self.assertEqual("SQL_TRANSLATION_FAILED", result["error_code"])
        self.assertFalse(result["retryable"])
        self.assertNotIn("semantic_validation_report", result)
        flattened = "\n".join(flatten_strings(result))
        for sentinel in _SENTINELS:
            self.assertNotIn(sentinel, flattened)

    def test_semantic_looking_runtime_error_is_not_trusted(self):
        # Only ValueError failures are catalog-produced confirmations. A non-ValueError
        # that merely echoes a semantic wording must stay sanitized.
        value = translator()

        def explode(*_args, **_kwargs):
            raise RuntimeError(f"不存在指标: injected by {_PRIVATE_SENTINEL}")

        value.translate = explode
        result = value.execute_query(json.dumps(base_ast()), "6")

        self.assertEqual("SQL_TRANSLATION_FAILED", result["error_code"])
        self.assertFalse(result["retryable"])
        self.assertNotIn("semantic_validation_report", result)
        flattened = "\n".join(flatten_strings(result))
        for sentinel in _SENTINELS:
            self.assertNotIn(sentinel, flattened)

    def test_translate_only_confirmed_semantic_failure_keeps_report(self):
        ast = base_ast(metrics=[{"name": "invented", "alias": "编造指标"}])
        result = translator().translate_only(json.dumps(ast, ensure_ascii=False), "6")

        self.assertFalse(result["success"])
        self.assertEqual("SEMANTIC_ASSET_NOT_FOUND", result["error_code"])
        self.assertFalse(result["retryable"])
        report = result["semantic_validation_report"]
        self.assertEqual("不存在指标: invented", report["errors"][0]["message"])
        self.assertIn("不存在指标: invented", result["error"])

    def test_translate_only_unknown_failure_is_sanitized_without_report(self):
        # /api/translate returns this branch directly; it must apply the same
        # sanitization contract as execute_query.
        value = translator()

        def explode(*_args, **_kwargs):
            raise RuntimeError(
                f"{_PRIVATE_SENTINEL} leaked {_FABRICATED_CREDENTIAL} "
                f"via {_FABRICATED_ADDRESS} sql={_FABRICATED_SQL}"
            )

        value.translate = explode
        result = value.translate_only(json.dumps(base_ast()), "6")

        self.assertFalse(result["success"])
        self.assertEqual("SEMANTIC_VALIDATION_FAILED", result["error_code"])
        self.assertFalse(result["retryable"])
        self.assertNotIn("semantic_validation_report", result)
        self.assertIn("内部处理错误", result["error"])
        flattened = "\n".join(flatten_strings(result))
        for sentinel in _SENTINELS:
            self.assertNotIn(sentinel, flattened)

    def test_value_error_with_semantic_wording_and_sentinel_is_degraded(self):
        # Acceptance probe: exception type + semantic wording must not make the
        # full raw text trustworthy. The identity suffix carries extra text, so it
        # cannot be safely extracted; the response degrades to the controlled
        # category message with no raw excerpt anywhere, while the existing mapped
        # error code classification is reused.
        sentinel_message = f"不存在指标: {_PRIVATE_SENTINEL} password=example"
        for entry in ("execute_query", "translate_only"):
            value = translator()
            value.translate = (
                lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    ValueError(sentinel_message)
                )
            )
            if entry == "execute_query":
                result = value.execute_query(json.dumps(base_ast()), "6")
                self.assertEqual("SQL_TRANSLATION_FAILED", result["error_code"])
            else:
                result = value.translate_only(json.dumps(base_ast()), "6")
                # The mapped code classification is reused even when the identifier
                # itself cannot be safely extracted.
                self.assertEqual("SEMANTIC_ASSET_NOT_FOUND", result["error_code"])
            self.assertFalse(result["retryable"])
            # Degraded controlled wording names the category and the admin hint,
            # never the raw exception text.
            self.assertIn("不存在于当前语义模型", result["error"])
            self.assertIn("标识无法安全提取", result["error"])
            self.assertNotIn(_PRIVATE_SENTINEL, result["error"])
            self.assertNotIn("password", result["error"])
            flattened = "\n".join(flatten_strings(result))
            self.assertNotIn(_PRIVATE_SENTINEL, flattened)
            self.assertNotIn("password=example", flattened)
            # The report, when kept, is generated from the same controlled
            # wording, never from the raw exception text.
            self.assertEqual(
                "引用的实体、指标、维度或过滤字段不存在于当前语义模型",
                result["semantic_validation_report"]["errors"][0]["message"],
            )


if __name__ == "__main__":
    unittest.main()


def test_sanitized_report_keeps_original_error_classification():
    from sql_translator_prod import SQLTranslatorProd
    result = SQLTranslatorProd._controlled_translation_failure(ValueError("\u4e0d\u5b58\u5728\u6307\u6807: bad value"), False)
    assert result["error_code"] == "SEMANTIC_ASSET_NOT_FOUND"
    assert result["semantic_validation_report"]["errors"][0]["code"] == result["error_code"]
