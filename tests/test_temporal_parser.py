from __future__ import annotations

from datetime import date

import pytest

import app.intent.classifier as classifier_module
from app.domain.models import TrustedIdentity
from app.intent import RuleBasedIntentClassifier


IDENTITY = TrustedIdentity(tenant_id="tenant-a", user_id="user-a")


def assert_range(text: str, expected_start: str, expected_end_exclusive: str) -> None:
    parsed = RuleBasedIntentClassifier._time_range(text)
    assert parsed is not None
    assert parsed.start.isoformat() == expected_start
    assert parsed.end_exclusive.isoformat() == expected_end_exclusive


@pytest.mark.parametrize(
    ("text", "expected_start", "expected_end_exclusive"),
    [
        ("分析2026年7月1号到30号的", "2026-07-01", "2026-07-31"),
        ("2026年7月1日到30日", "2026-07-01", "2026-07-31"),
        ("2026年7月1号至7月30号", "2026-07-01", "2026-07-31"),
        ("2026-07-01至07-30", "2026-07-01", "2026-07-31"),
        ("2026-07-01至2026-07-30", "2026-07-01", "2026-07-31"),
        ("2026/07/01 到 2026/07/30", "2026-07-01", "2026-07-31"),
        ("2026.07.01~2026.07.30", "2026-07-01", "2026-07-31"),
        ("那查询一下25年12月1号到31号的", "2025-12-01", "2026-01-01"),
        ("2024年2月29号", "2024-02-29", "2024-03-01"),
    ],
)
def test_explicit_date_forms_are_normalized_to_right_open_range(
    text: str, expected_start: str, expected_end_exclusive: str
) -> None:
    assert_range(text, expected_start, expected_end_exclusive)


@pytest.mark.parametrize(
    ("text", "expected_start", "expected_end_exclusive"),
    [
        ("查询2026年7月销售额", "2026-07-01", "2026-08-01"),
        ("查询2026年12月销售额", "2026-12-01", "2027-01-01"),
    ],
)
def test_bare_year_month_means_the_whole_calendar_month(
    text: str, expected_start: str, expected_end_exclusive: str
) -> None:
    assert_range(text, expected_start, expected_end_exclusive)


@pytest.mark.parametrize(
    "text",
    [
        "2026年2月30号到3月2号",
        "2026-07-01至2026-02-30",
        "2026年7月30号到1号",
        "2026年12月1号到1月1号",
        "2026年13月",
        "最近0天",
    ],
)
def test_invalid_or_backwards_ranges_are_rejected(text: str) -> None:
    assert RuleBasedIntentClassifier._time_range(text) is None


def test_future_dates_are_valid_input() -> None:
    assert_range("分析2099年7月1号到30号", "2099-07-01", "2099-07-31")


def test_classification_no_longer_asks_for_an_already_supplied_time_range() -> None:
    request = RuleBasedIntentClassifier().classify(
        "分析2026年7月1号到30号的", IDENTITY, "conversation-1"
    )

    assert request.time_range is not None
    assert request.time_range.start.isoformat() == "2026-07-01"
    assert request.time_range.end_exclusive.isoformat() == "2026-07-31"
    assert request.missing_slots == ["metric"]


@pytest.mark.parametrize(
    ("question", "start", "end"),
    [
        ("查询2026年订单明细", "2026-01-01", "2027-01-01"),
        ("查询2026年度订单明细", "2026-01-01", "2027-01-01"),
        ("分析2024年至2026年销售额", "2024-01-01", "2027-01-01"),
    ],
)
def test_whole_calendar_year_is_a_complete_time_range(question, start, end) -> None:
    parsed = RuleBasedIntentClassifier._time_range(question)
    assert parsed is not None
    assert parsed.start.isoformat() == start
    assert parsed.end_exclusive.isoformat() == end


class FrozenDate(date):
    @classmethod
    def today(cls) -> "FrozenDate":
        return cls(2026, 8, 20)


@pytest.mark.parametrize(
    ("text", "expected_start", "expected_end_exclusive"),
    [
        ("最近30天", "2026-07-22", "2026-08-21"),
        ("本月", "2026-08-01", "2026-09-01"),
        ("这个月", "2026-08-01", "2026-09-01"),
        ("上月", "2026-07-01", "2026-08-01"),
        ("上个月", "2026-07-01", "2026-08-01"),
        ("今天", "2026-08-20", "2026-08-21"),
        ("昨天", "2026-08-19", "2026-08-20"),
        ("今年", "2026-01-01", "2027-01-01"),
        ("去年", "2025-01-01", "2026-01-01"),
        ("本季度", "2026-07-01", "2026-10-01"),
        ("上季度", "2026-04-01", "2026-07-01"),
        ("下个月", "2026-09-01", "2026-10-01"),
    ],
)
def test_existing_relative_period_behavior_is_preserved(
    monkeypatch: pytest.MonkeyPatch,
    text: str,
    expected_start: str,
    expected_end_exclusive: str,
) -> None:
    monkeypatch.setattr(classifier_module, "date", FrozenDate)
    assert_range(text, expected_start, expected_end_exclusive)


@pytest.mark.parametrize("text", ("截至今天", "截止今天", "截至今日", "截止今日"))
def test_bare_cutoff_today_is_an_executable_year_to_date_range(
    monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    monkeypatch.setattr(classifier_module, "date", FrozenDate)
    assert_range(text, "2026-01-01", "2026-08-21")
