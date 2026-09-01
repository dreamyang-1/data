import pytest

from app.analysis.forecasting import DeterministicForecastSelector, ForecastModelError


def test_linear_history_selects_linear_model_by_rolling_backtest() -> None:
    result = DeterministicForecastSelector().fit_predict(
        [10, 12, 14, 16, 18, 20], non_negative=True
    )
    assert result.model_name == "linear_drift"
    assert result.forecast == pytest.approx(22)
    assert result.validation_mae == pytest.approx(0)


def test_level_shift_prefers_naive_when_it_backtests_better() -> None:
    result = DeterministicForecastSelector().fit_predict(
        [10, 10, 10, 30, 30, 30, 30, 30], non_negative=True
    )
    assert result.model_name == "naive_last"
    assert result.forecast == 30


def test_non_negative_metric_clamps_lower_bound_and_forecast() -> None:
    result = DeterministicForecastSelector().fit_predict(
        [10, 8, 6, 4, 2, 1], non_negative=True
    )
    assert result.forecast >= 0
    assert result.lower >= 0


def test_model_selection_rejects_short_or_non_finite_history() -> None:
    with pytest.raises(ForecastModelError):
        DeterministicForecastSelector().fit_predict([1, 2, 3], non_negative=True)
    with pytest.raises(ForecastModelError):
        DeterministicForecastSelector().fit_predict(
            [1, 2, 3, 4, 5, float("nan")], non_negative=True
        )
