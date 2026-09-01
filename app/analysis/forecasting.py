from __future__ import annotations

import math
import re
import statistics
from dataclasses import dataclass
from typing import Callable


class ForecastModelError(ValueError):
    pass


@dataclass(frozen=True)
class ForecastResult:
    model_name: str
    forecast: float
    lower: float
    upper: float
    validation_mae: float
    validation_mape: float | None
    validation_points: int
    candidate_scores: dict[str, dict[str, float | None]]
    diagnostics: dict[str, float | int | str | None]
    warnings: list[str]


class DeterministicForecastSelector:
    """Select a one-step model using expanding-window backtesting.

    This component has no LLM dependency. Candidate models are deterministic,
    selection is based on out-of-sample MAE, and the interval is calibrated
    from held-out absolute errors rather than invented by a language model.
    """

    VERSION = "forecast-selector-v1"

    def __init__(self, *, minimum_points: int = 6, validation_fraction: float = 0.33):
        self.minimum_points = minimum_points
        self.validation_fraction = validation_fraction

    def fit_predict(self, values: list[float], *, non_negative: bool) -> ForecastResult:
        if len(values) < self.minimum_points:
            raise ForecastModelError(
                f"forecast requires at least {self.minimum_points} ordered observations"
            )
        if not all(math.isfinite(value) for value in values):
            raise ForecastModelError("forecast values must all be finite")

        validation_points = max(2, min(len(values) - 3, round(len(values) * self.validation_fraction)))
        validation_start = len(values) - validation_points
        candidates: dict[str, Callable[[list[float]], float]] = {
            "naive_last": self._naive,
            "recent_mean_3": self._recent_mean,
            "linear_drift": self._linear,
        }
        scores: dict[str, dict[str, float | None]] = {}
        errors_by_model: dict[str, list[float]] = {}
        for name, predictor in candidates.items():
            errors: list[float] = []
            percentage_errors: list[float] = []
            for index in range(validation_start, len(values)):
                predicted = predictor(values[:index])
                actual = values[index]
                errors.append(actual - predicted)
                if actual != 0:
                    percentage_errors.append(abs(actual - predicted) / abs(actual))
            errors_by_model[name] = errors
            scores[name] = {
                "mae": statistics.mean(abs(error) for error in errors),
                "mape": statistics.mean(percentage_errors) if percentage_errors else None,
            }

        # Stable tie-breaking prefers the simpler model, reducing variance on
        # short histories. Dict insertion order is intentional here.
        selected = min(candidates, key=lambda name: float(scores[name]["mae"] or 0.0))
        forecast = candidates[selected](values)
        if non_negative:
            forecast = max(0.0, forecast)

        absolute_errors = sorted(abs(error) for error in errors_by_model[selected])
        interval_error = self._upper_quantile(absolute_errors, 0.90)
        lower = forecast - interval_error
        upper = forecast + interval_error
        if non_negative:
            lower = max(0.0, lower)

        mean_value = statistics.mean(values)
        total_variance = sum((value - mean_value) ** 2 for value in values)
        x_mean = (len(values) - 1) / 2
        denominator = sum((index - x_mean) ** 2 for index in range(len(values)))
        slope = (
            0.0
            if denominator == 0
            else sum(
                (index - x_mean) * (value - mean_value)
                for index, value in enumerate(values)
            )
            / denominator
        )
        intercept = mean_value - slope * x_mean
        fitted = [intercept + slope * index for index in range(len(values))]
        residual_variance = sum(
            (actual - estimate) ** 2 for actual, estimate in zip(values, fitted)
        )
        r_squared = None if total_variance == 0 else 1 - residual_variance / total_variance
        warnings = [
            "预测仅基于历史数值，不包含节假日、促销、价格或外部事件变量"
        ]
        if validation_points < 4:
            warnings.append("回测窗口少于4期，模型选择和区间稳定性有限")
        selected_mape = scores[selected]["mape"]
        if selected_mape is not None and selected_mape > 0.30:
            warnings.append("滚动回测MAPE超过30%，预测只适合作为低置信参考")
        if interval_error == 0 and len(set(values)) > 1:
            warnings.append("回测残差样本过少，预测区间可能偏窄")
        if len(set(values)) == 1:
            warnings.append("历史序列为常数，算法只能延续当前水平，不能证明未来稳定")

        return ForecastResult(
            model_name=selected,
            forecast=forecast,
            lower=lower,
            upper=upper,
            validation_mae=float(scores[selected]["mae"] or 0.0),
            validation_mape=selected_mape,
            validation_points=validation_points,
            candidate_scores=scores,
            diagnostics={
                "history_points": len(values),
                "linear_r_squared_reference": r_squared,
                "linear_slope_reference": slope,
                "interval_method": "rolling_backtest_absolute_error_p90",
                "algorithm_version": self.VERSION,
            },
            warnings=warnings,
        )

    @staticmethod
    def _naive(history: list[float]) -> float:
        return history[-1]

    @staticmethod
    def _recent_mean(history: list[float]) -> float:
        return statistics.mean(history[-min(3, len(history)):])

    @staticmethod
    def _linear(history: list[float]) -> float:
        if len(history) < 2:
            return history[-1]
        x_mean = (len(history) - 1) / 2
        y_mean = statistics.mean(history)
        denominator = sum((index - x_mean) ** 2 for index in range(len(history)))
        if denominator == 0:
            return history[-1]
        slope = sum(
            (index - x_mean) * (value - y_mean)
            for index, value in enumerate(history)
        ) / denominator
        intercept = y_mean - slope * x_mean
        return intercept + slope * len(history)

    @staticmethod
    def _upper_quantile(values: list[float], probability: float) -> float:
        if not values:
            return 0.0
        index = max(0, min(len(values) - 1, math.ceil(probability * len(values)) - 1))
        return values[index]


def assess_time_axis(labels: list[str]) -> tuple[str | None, bool]:
    """Infer common time grain and verify that adjacent periods are regular."""
    if all(re.fullmatch(r"\d{4}-\d{2}", label) for label in labels):
        ordinals = [int(label[:4]) * 12 + int(label[5:7]) for label in labels]
        return "month", all(right - left == 1 for left, right in zip(ordinals, ordinals[1:]))
    if all(re.fullmatch(r"\d{4}-?[Qq][1-4]", label) for label in labels):
        ordinals = [int(label[:4]) * 4 + int(label[-1]) for label in labels]
        return "quarter", all(right - left == 1 for left, right in zip(ordinals, ordinals[1:]))
    if all(re.fullmatch(r"\d{4}-?[Ww]\d{2}", label) for label in labels):
        # Ordering and ISO-week validity have already been checked by AnalysisEngine.
        keys = [(int(label[:4]), int(label[-2:])) for label in labels]
        regular = all(
            (right_year == left_year and right_week == left_week + 1)
            or (right_year == left_year + 1 and right_week == 1 and left_week >= 52)
            for (left_year, left_week), (right_year, right_week) in zip(keys, keys[1:])
        )
        return "week", regular
    if all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", label) for label in labels):
        from datetime import date

        dates = [date.fromisoformat(label) for label in labels]
        return "day", all((right - left).days == 1 for left, right in zip(dates, dates[1:]))
    if all(re.fullmatch(r"\d{4}", label) for label in labels):
        years = [int(label) for label in labels]
        return "year", all(right - left == 1 for left, right in zip(years, years[1:]))
    return None, False
