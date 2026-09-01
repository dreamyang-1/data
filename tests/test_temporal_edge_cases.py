from datetime import date

import pytest

import app.intent.classifier as classifier_module
from app.intent import RuleBasedIntentClassifier


class FrozenDate(date):
    @classmethod
    def today(cls) -> "FrozenDate":
        return cls(2026, 8, 20)


def assert_range(text: str, start: str, end_exclusive: str) -> None:
    parsed = RuleBasedIntentClassifier._time_range(text)
    assert parsed is not None
    assert parsed.start.isoformat() == start
    assert parsed.end_exclusive.isoformat() == end_exclusive


@pytest.mark.parametrize(
    ("text", "start", "end"),
    [
        ("今年上半年", "2026-01-01", "2026-07-01"),
        ("今年下半年", "2026-07-01", "2027-01-01"),
        ("本月至今", "2026-08-01", "2026-08-21"),
        ("本月截至昨天", "2026-08-01", "2026-08-20"),
        ("今年截至昨天", "2026-01-01", "2026-08-20"),
        ("2026年7月至8月", "2026-07-01", "2026-09-01"),
        ("2026-7", "2026-07-01", "2026-08-01"),
        ("7月份", "2026-07-01", "2026-08-01"),
        ("过去30天", "2026-07-22", "2026-08-21"),
        ("近7日", "2026-08-14", "2026-08-21"),
        ("最近7个自然日", "2026-08-14", "2026-08-21"),
        ("最近一周", "2026-08-14", "2026-08-21"),
        ("过去两周", "2026-08-07", "2026-08-21"),
        ("最近一个月", "2026-07-20", "2026-08-21"),
        ("近三个月", "2026-05-20", "2026-08-21"),
        ("最近半年", "2026-02-20", "2026-08-21"),
        ("过去一年", "2025-08-20", "2026-08-21"),
        ("本周", "2026-08-17", "2026-08-24"),
        ("上周", "2026-08-10", "2026-08-17"),
        ("2026Q2", "2026-04-01", "2026-07-01"),
        ("第二季度", "2026-04-01", "2026-07-01"),
        ("2026年7月1日起至今", "2026-07-01", "2026-08-21"),
    ],
)
def test_common_temporal_edges_are_normalized(
    monkeypatch: pytest.MonkeyPatch, text: str, start: str, end: str
):
    monkeypatch.setattr(classifier_module, "date", FrozenDate)
    assert_range(text, start, end)


@pytest.mark.parametrize(
    "text",
    [
        "最近400天",
        "过去999天",
        "2026年7月30号到1号",
        "2026年7月截至",
    ],
)
def test_unsupported_or_invalid_temporal_expression_is_not_partially_parsed(
    monkeypatch: pytest.MonkeyPatch, text: str
):
    monkeypatch.setattr(classifier_module, "date", FrozenDate)
    assert RuleBasedIntentClassifier._time_range(text) is None
