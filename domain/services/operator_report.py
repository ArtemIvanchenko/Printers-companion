"""Compact, deterministic report for the operator's print card.

This is deliberately a presentation layer over already-computed analytics. It
does not invent another anomaly model and never presents a possible cause as a
proven causal diagnosis.
"""
from __future__ import annotations

import math
from typing import Any

from core.versioning.provenance import build_provenance
from profiles.signal_catalog import signal_display_name


_SEVERITY_RANK = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
_CLASSIFICATION_RU = {
    "REAL_PRINT": "Печать завершена",
    "REAL_PRINT_WITH_RESUME": "Печать завершена после возобновления",
    "PRE_BURN_SESSION": "Подготовительный прожиг",
    "SERVICE_SESSION": "Сервисная сессия",
    "IDLE_DIAGNOSTIC": "Диагностическая сессия",
    "MAINTENANCE_WINDOW": "Обслуживание",
    "INCOMPLETE_OR_UNKNOWN": "Сессия распознана не полностью",
}
_CAUSES = {
    "oxygen": ["негерметичность камеры", "нестабильная подача защитного газа", "ошибка датчика кислорода"],
    "кислород": ["негерметичность камеры", "нестабильная подача защитного газа", "ошибка датчика кислорода"],
    "humidity": ["недостаточная осушка газа", "влага в порошке или газовой магистрали", "ошибка датчика влажности"],
    "влажность": ["недостаточная осушка газа", "влага в порошке или газовой магистрали", "ошибка датчика влажности"],
    "temperature": ["неравномерный нагрев", "изменение тепловой нагрузки детали", "ошибка температурного датчика"],
    "температура": ["неравномерный нагрев", "изменение тепловой нагрузки детали", "ошибка температурного датчика"],
    "pressure": ["утечка или изменение подачи газа", "загрязнение газового контура", "ошибка датчика давления"],
    "давление": ["утечка или изменение подачи газа", "загрязнение газового контура", "ошибка датчика давления"],
}


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        numeric = float(value)
        return numeric if math.isfinite(numeric) else None
    return None


def _severity(value: Any) -> str:
    clean = str(value or "medium").lower()
    return clean if clean in _SEVERITY_RANK else "medium"


def _causes_for(*values: Any) -> list[str]:
    text = " ".join(str(value or "").lower() for value in values)
    for key, causes in _CAUSES.items():
        if key in text:
            return causes
    return ["изменение режима процесса", "особенность геометрии слоя", "ошибка или пропуск измерений"]


def _recommendation_for(*values: Any) -> str:
    text = " ".join(str(value or "").lower() for value in values)
    if "oxygen" in text or "кислород" in text:
        return "Проверить герметичность, подачу защитного газа и показания обоих датчиков кислорода."
    if "humid" in text or "влаж" in text or "росы" in text:
        return "Проверить осушитель, газовую магистраль и условия хранения порошка."
    if "temper" in text or "темпера" in text or "теплов" in text:
        return "Сопоставить участок с геометрией детали и проверить нагреватели и температурные датчики."
    if "press" in text or "давлен" in text or "фильтр" in text:
        return "Проверить герметичность, газовый тракт, фильтры и датчики давления."
    return "Сопоставить участок с геометрией детали и журналом действий оператора."


def _latest_final_outcome(outcomes: list[dict[str, Any]]) -> dict[str, Any] | None:
    finals = [
        row for row in outcomes
        # Legacy report payloads predate the explicit flag; their accepted /
        # rejected rows were already treated as final. A stored new row always
        # carries True/False, so generic observations remain excluded.
        if row.get("is_final", True) and row.get("result") in {"accepted", "rejected"}
    ]
    if not finals:
        return None
    # Repository order is newest first; sorting makes the pure function safe for
    # callers that pass arbitrary order.
    return max(finals, key=lambda row: (str(row.get("timestamp") or ""), str(row.get("outcome_id") or "")))


def build_operator_report(
    *,
    session_id: str | None,
    group: dict[str, Any] | None,
    quality_outcomes: list[dict[str, Any]] | None = None,
    print_record: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Summarize one print/session into an actionable, Russian-language view."""
    group = group or {}
    outcomes = quality_outcomes or []
    features = group.get("features") or {}
    health = group.get("health") or {}
    data_quality = group.get("data_quality") or {}
    deviations: list[dict[str, Any]] = []

    def add(
        code: str,
        title: str,
        detail: str,
        *,
        severity: str = "medium",
        causes: list[str] | None = None,
        recommendation: str | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> None:
        deviations.append({
            "code": code,
            "title": title,
            "severity": _severity(severity),
            "detail": detail,
            "possible_causes": causes or _causes_for(code, title, detail),
            "recommendation": recommendation or _recommendation_for(code, title, detail),
            "evidence": evidence or {},
        })

    dq_score = _number(data_quality.get("score"))
    if dq_score is None:
        dq_score = _number(features.get("data_quality_score"))
    for issue in data_quality.get("issues") or []:
        add(
            f"data_quality:{issue.get('kind') or 'unknown'}",
            "Неполные или сомнительные исходные данные",
            str(issue.get("detail") or "Обнаружена проблема качества логов."),
            severity=issue.get("severity") or "medium",
            causes=["неполное копирование логов", "прерывание записи", "неисправность или зависание датчика"],
            recommendation="Проверить комплектность исходных логов; выводы аналитики считать предварительными.",
            evidence={"count": issue.get("count")},
        )

    readiness = health.get("readiness") or {}
    readiness_score = _number(readiness.get("score"))
    if readiness_score is not None and readiness_score < 75:
        severity = "high" if readiness_score < 50 else "medium"
        add(
            "atmosphere_readiness",
            "Недостаточная устойчивость защитной атмосферы",
            f"Оценка готовности атмосферы: {readiness_score:.1f} из 100.",
            severity=severity,
            causes=["нестабильность кислорода", "нестабильность давления", "повышенная или нестабильная влажность"],
            recommendation="Проверить продувку, герметичность камеры, газ и осушитель до следующего запуска.",
            evidence={"score": readiness_score, "factors": readiness.get("factors") or {}},
        )

    for anomaly in health.get("anomalies") or []:
        signal = str(anomaly.get("signal") or "")
        display = signal_display_name(signal, include_code=True) if signal else str(anomaly.get("semantic") or "Параметр")
        add(
            f"process:{anomaly.get('kind') or 'anomaly'}:{signal or 'unknown'}",
            f"Отклонение: {display}",
            str(anomaly.get("detail") or "Обнаружено статистическое отклонение процесса."),
            severity=anomaly.get("severity") or "medium",
            causes=_causes_for(signal, anomaly.get("semantic")),
            recommendation=_recommendation_for(signal, anomaly.get("semantic")),
            evidence={key: anomaly.get(key) for key in ("value", "z_score", "alarm_fraction") if anomaly.get(key) is not None},
        )

    burn = health.get("burn_drift") or {}
    if burn.get("trend") == "rising":
        relative = _number(burn.get("relative_change_pct"))
        detail = "Время лазерной обработки увеличивается к концу печати"
        if relative is not None:
            detail += f" примерно на {relative:.1f}%"
        add(
            "burn_time_rising",
            "Рост времени обработки слоя",
            detail + ".",
            severity="medium",
            causes=["рост площади или сложности сечений детали", "изменение режима сканирования", "паузы или нестабильность процесса"],
            recommendation="Сопоставить тренд с площадью сечений модели; проверить участки с выбросами по слоям.",
            evidence={
                "slope_sec_per_layer": burn.get("slope_sec_per_layer"),
                "relative_change_pct": burn.get("relative_change_pct"),
            },
        )
    outlier_layers = burn.get("outlier_layers") or []
    if outlier_layers:
        layers = [row.get("layer") for row in outlier_layers if row.get("layer") is not None]
        add(
            "burn_time_outliers",
            "Нетипичное время обработки отдельных слоёв",
            "Слои: " + ", ".join(map(str, layers[:12])) + ("…" if len(layers) > 12 else ""),
            severity="medium",
            causes=["особенность геометрии слоя", "остановка или повтор операции", "сбой измерения времени"],
            recommendation="Проверить указанные высоты в модели, события и паузы в журнале.",
            evidence={"layers": layers[:50]},
        )

    for metric in (group.get("soft_sensors") or {}).get("metrics") or []:
        if metric.get("status") != "warning":
            continue
        add(
            f"soft_sensor:{metric.get('key') or 'unknown'}",
            str(metric.get("name_ru") or "Отклонение расчётного показателя"),
            f"Значение: {metric.get('value')} {metric.get('unit') or ''}".strip(),
            severity="medium",
            causes=_causes_for(metric.get("key"), metric.get("name_ru")),
            recommendation=_recommendation_for(metric.get("key"), metric.get("name_ru")),
            evidence={
                "value": metric.get("value"),
                "p95": metric.get("p95"),
                "confidence": metric.get("confidence"),
                "inputs": metric.get("inputs") or [],
            },
        )

    pause_count = features.get("pause_count")
    if isinstance(pause_count, int) and pause_count > 0:
        add(
            "print_pauses",
            "Во время печати были паузы",
            f"Количество зарегистрированных пауз: {pause_count}.",
            severity="low",
            causes=["действие оператора", "автоматическое ожидание оборудования", "восстановление после предупреждения"],
            recommendation="Проверить журнал событий около каждой паузы и состояние детали после возобновления.",
            evidence={"pause_count": pause_count, "idle_min": features.get("idle_min")},
        )

    deviations.sort(key=lambda row: (-_SEVERITY_RANK[row["severity"]], row["code"]))

    latest = _latest_final_outcome(outcomes)
    classification = str(group.get("classification") or "INCOMPLETE_OR_UNKNOWN")
    classification_confidence = _number(group.get("confidence")) or 0.0
    if classification_confidence > 1:
        classification_confidence /= 100
    data_factor = (dq_score / 100) if dq_score is not None else 0.35
    confidence_score = round(max(0.0, min(1.0, 0.65 * data_factor + 0.35 * classification_confidence)), 2)
    confidence_level = "высокая" if confidence_score >= 0.8 else "средняя" if confidence_score >= 0.55 else "низкая"

    if latest and latest.get("result") == "rejected":
        state_code, state_label, state_severity = "defect_confirmed", "Контролем подтверждён брак", "critical"
    elif dq_score is not None and dq_score < 50:
        state_code, state_label, state_severity = "insufficient_data", "Недостаточно надёжных данных для уверенного вывода", "high"
    elif latest and latest.get("result") == "accepted" and deviations:
        state_code, state_label, state_severity = "accepted_with_observations", "Годность подтверждена, есть замечания по процессу", "medium"
    elif any(_SEVERITY_RANK[row["severity"]] >= _SEVERITY_RANK["high"] for row in deviations):
        state_code, state_label, state_severity = "attention_required", "Требуется проверка отклонений", "high"
    elif deviations:
        state_code, state_label, state_severity = "review_recommended", "Печать завершена, есть замечания", "medium"
    elif latest and latest.get("result") == "accepted":
        state_code, state_label, state_severity = "accepted", "Годность подтверждена контролем", "info"
    elif classification in {"REAL_PRINT", "REAL_PRINT_WITH_RESUME"}:
        state_code, state_label, state_severity = "no_significant_deviations", "Существенных отклонений не обнаружено", "info"
    else:
        state_code, state_label, state_severity = "not_a_confirmed_print", _CLASSIFICATION_RU.get(classification, classification), "info"

    recommendations = []
    for deviation in deviations:
        value = deviation["recommendation"]
        if value and value not in recommendations:
            recommendations.append(value)
    if session_id is None:
        recommendations.append("Загрузить и привязать логи печати, чтобы рассчитать отклонения и уверенность анализа.")
    if not latest and classification in {"REAL_PRINT", "REAL_PRINT_WITH_RESUME"}:
        recommendations.append("После контроля детали зафиксировать итог «годная» или «брак» в карточке печати.")
    if not recommendations:
        recommendations.append("Дополнительных действий по данным автоматического анализа не требуется.")

    cause_rows: list[dict[str, str]] = []
    for deviation in deviations:
        for cause in deviation["possible_causes"]:
            if any(row["cause"] == cause for row in cause_rows):
                continue
            cause_rows.append({
                "cause": cause,
                "basis": deviation["title"],
                "confidence": "гипотеза",
            })

    quality_label = None
    if latest:
        quality_label = {
            "result": latest.get("result"),
            "result_ru": "годная" if latest.get("result") == "accepted" else "брак",
            "inspection_type": latest.get("inspection_type"),
            "inspection_result": latest.get("inspection_result"),
            "defect_type": latest.get("defect_type"),
            "defect_location": latest.get("defect_location"),
            "layer_range": latest.get("layer_range"),
            "notes": latest.get("notes"),
            "created_by": latest.get("created_by"),
            "timestamp": latest.get("timestamp"),
            "supersedes_outcome_id": latest.get("supersedes_outcome_id"),
        }

    report = {
        "schema_version": "operator-report-1.0",
        "print_record_id": (print_record or {}).get("record_id"),
        "print_name": (print_record or {}).get("name"),
        "session_id": session_id,
        "classification": {
            "code": classification,
            "label_ru": _CLASSIFICATION_RU.get(classification, classification),
        },
        "state": {"code": state_code, "label_ru": state_label, "severity": state_severity},
        "confidence": {
            "score": confidence_score,
            "level_ru": confidence_level,
            "data_quality_score": dq_score,
            "classification_confidence": round(classification_confidence, 2),
            "note": "Уверенность отражает полноту логов и надёжность классификации, а не вероятность годности детали.",
        },
        "deviations": deviations[:20],
        "possible_causes": cause_rows[:12],
        "recommendations": recommendations[:12],
        "quality_outcome": quality_label,
        "disclaimer": "Возможные причины являются проверяемыми гипотезами, а не доказанной причинностью.",
    }
    report["version_metadata"] = build_provenance(
        "operator_report",
        inputs={
            "session_id": session_id,
            "print_record_id": (print_record or {}).get("record_id"),
            "print_revision": (print_record or {}).get("revision"),
            "quality_outcomes": [
                {
                    "outcome_id": row.get("outcome_id"),
                    "timestamp": row.get("timestamp"),
                    "result": row.get("result"),
                }
                for row in outcomes
            ],
        },
        config={"schema_version": report["schema_version"]},
    )
    return report


__all__ = ["build_operator_report"]
