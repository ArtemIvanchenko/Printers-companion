"""Conservative, explainable evidence score for ``print card ↔ log session``.

Dates are useful for generating candidates, but never prove identity: build
files are often prepared a day before printing and several jobs may share a
date.  Layer-count agreement is the strongest generally available independent
signal.  A previously confirmed geometry/profile fingerprint can strengthen a
repeat, while a material contradiction or a gross layer mismatch rejects it.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PairMatchScore:
    score: float
    confidence: str
    eligible: bool
    auto_link_allowed: bool
    reasons_ru: list[str] = field(default_factory=list)
    warnings_ru: list[str] = field(default_factory=list)
    layer_error_pct: float | None = None

    def as_dict(self) -> dict:
        return {
            "score": round(self.score, 1),
            "confidence": self.confidence,
            "eligible": self.eligible,
            "auto_link_allowed": self.auto_link_allowed,
            "layer_error_pct": round(self.layer_error_pct, 3)
            if self.layer_error_pct is not None else None,
            "reasons_ru": list(self.reasons_ru),
            "warnings_ru": list(self.warnings_ru),
        }


def _normalise_material(value: str | None) -> str | None:
    if not value:
        return None
    text = value.casefold().strip()
    if text in {"—", "unknown", "неизвестно"}:
        return None
    if "steel" in text or "стал" in text:
        return "steel"
    if "alum" in text or "алюм" in text:
        return "aluminum"
    return text


def score_print_session_pair(
    *,
    date_delta_hours: float | None,
    expected_layers: int | None,
    observed_last_layer: int | None,
    expected_material: str | None = None,
    observed_material: str | None = None,
    explicit_import_hint: bool = False,
    geometry_fingerprint_match: bool = False,
    repeated_burn_profile_similarity: float | None = None,
) -> PairMatchScore:
    """Score independent evidence without using predicted duration as a label.

    Duration is intentionally absent: choosing a pair because it agrees with
    the current time model and then using that same pair to validate the model
    is circular data leakage.
    """
    score = 0.0
    reasons: list[str] = []
    warnings: list[str] = []
    hard_reject = False
    strong_identity_signal = False
    layer_error_pct = None

    expected_mat = _normalise_material(expected_material)
    observed_mat = _normalise_material(observed_material)
    if expected_mat and observed_mat:
        if expected_mat != observed_mat:
            hard_reject = True
            warnings.append("Материал карточки противоречит материалу сессии.")
        else:
            score += 8
            reasons.append("Материал совпадает.")

    if expected_layers and observed_last_layer and expected_layers > 0 and observed_last_layer > 0:
        layer_error_pct = abs(expected_layers - observed_last_layer) / expected_layers * 100
        strong_identity_signal = True
        if layer_error_pct <= 0.5:
            score += 55
            reasons.append("Число слоёв совпадает с точностью до 0,5%.")
        elif layer_error_pct <= 1.0:
            score += 50
            reasons.append("Число слоёв совпадает с точностью до 1%.")
        elif layer_error_pct <= 2.0:
            score += 42
            reasons.append("Число слоёв совпадает с точностью до 2%.")
        elif layer_error_pct <= 5.0:
            score += 24
            warnings.append("Число слоёв отличается на 2–5%; нужна проверка краевых слоёв.")
        elif layer_error_pct <= 10.0:
            score += 6
            warnings.append("Число слоёв отличается на 5–10%; автоматически не связывать.")
        else:
            hard_reject = True
            warnings.append("Число слоёв расходится более чем на 10%.")
    else:
        warnings.append("Нет независимого сравнения числа слоёв.")

    if date_delta_hours is not None:
        delta = abs(date_delta_hours)
        if delta <= 12:
            score += 25
            reasons.append("Дата и время отличаются не более чем на 12 часов.")
        elif delta <= 36:
            score += 20
            reasons.append("Печать началась не позднее 36 часов от даты карточки.")
        elif delta <= 72:
            score += 12
            reasons.append("Печать попадает в трёхсуточное окно карточки.")
        elif delta <= 24 * 7:
            score += 4
            warnings.append("Между подготовкой модели и печатью прошло до недели.")
        else:
            warnings.append("Дата является слабым или противоречивым признаком.")

    if explicit_import_hint:
        score += 35
        strong_identity_signal = True
        reasons.append("Логи загружены оператором непосредственно из этой карточки.")
    if geometry_fingerprint_match:
        score += 35
        strong_identity_signal = True
        reasons.append("Совпадает контрольная сумма ранее подтверждённой компоновки.")
    if repeated_burn_profile_similarity is not None:
        similarity = max(-1.0, min(1.0, repeated_burn_profile_similarity))
        if similarity >= 0.98:
            score += 30
            strong_identity_signal = True
            reasons.append("Послойный профиль прожига почти идентичен подтверждённому повтору.")
        elif similarity >= 0.90:
            score += 18
            reasons.append("Послойный профиль прожига похож на подтверждённую печать.")
        elif similarity < 0.50:
            warnings.append("Послойный профиль прожига не похож на предполагаемый повтор.")

    score = min(score, 100.0)
    if hard_reject:
        confidence = "rejected"
    elif score >= 70 and strong_identity_signal:
        confidence = "strong"
    elif score >= 45:
        confidence = "plausible"
    else:
        confidence = "weak"
    eligible = not hard_reject
    return PairMatchScore(
        score=score,
        confidence=confidence,
        eligible=eligible,
        auto_link_allowed=eligible and confidence == "strong",
        reasons_ru=reasons,
        warnings_ru=warnings,
        layer_error_pct=layer_error_pct,
    )


__all__ = ["PairMatchScore", "score_print_session_pair"]
