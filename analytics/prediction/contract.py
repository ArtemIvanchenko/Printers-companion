"""Unified result contract for every predictor in ``analytics.prediction``.

Each predictor (print time, defect risk, maintenance forecast, cost) has its
own domain-specific result shape (``PrintTimeEstimate``, a plain risk dict,
a list of forecast dicts, ``CostEstimate``) and callers that already depend on
those specific keys keep working unchanged. ``PredictionResult`` is a second,
common shape attached alongside the existing one (as a ``prediction`` field or
dict key) so a caller that only needs "how much, and how trustworthy" does not
have to learn five different internal vocabularies.

The interval field is deliberately optional: filling it honestly (from real
predicted-vs-actual history, per predictor) is a separate step. Until then it
stays ``None`` rather than being guessed at.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class PredictionSource(str, Enum):
    """Where a value actually came from — shown to the operator so a number
    is never presented as more authoritative than it is.

    * CALCULATED — прямой расчёт по геометрии/формуле/паспортным данным, без
      обучения на истории печатей (например физика по паспортным скоростям).
    * CALIBRATED — тот же расчёт, скорректированный коэффициентом или медианой,
      выученными из истории предсказано/факт (например time_correction_by_mat).
    * MODEL — подогнанная статистическая/ML модель с параметрами, оценённая на
      исторических данных (NNLS-модель скана, логрегрессия/LightGBM риска брака,
      Theil-Sen тренд дрейфа сигнала).
    * HEURISTIC — прозрачная взвешенная эвристика без обучения на данных.
    * DEFAULT — захардкоженная константа-заглушка: нет ни расчёта, ни калибровки.
    """

    CALCULATED = "calculated"
    CALIBRATED = "calibrated"
    MODEL = "model"
    HEURISTIC = "heuristic"
    DEFAULT = "default"


@dataclass
class PredictionResult:
    value: float
    unit: str
    source: PredictionSource
    # (low, high) — None when not enough predicted-vs-actual history exists to
    # compute one honestly. Never fabricated as a fixed percentage of value.
    interval: tuple[float, float] | None = None
    # Size of the data the value/interval were derived from — pairs, sessions,
    # layers, labelled outcomes, whatever unit fits the predictor. None when
    # the predictor genuinely has no notion of a sample (e.g. a hardcoded
    # default, or when the underlying count isn't available at this call site).
    sample_size: int | None = None
    warnings: list[str] = field(default_factory=list)
    explanation: str = ""

    def to_dict(self) -> dict:
        return {
            "value": self.value,
            "unit": self.unit,
            "source": self.source.value,
            "interval": list(self.interval) if self.interval is not None else None,
            "sample_size": self.sample_size,
            "warnings": list(self.warnings),
            "explanation": self.explanation,
        }


__all__ = ["PredictionSource", "PredictionResult"]
