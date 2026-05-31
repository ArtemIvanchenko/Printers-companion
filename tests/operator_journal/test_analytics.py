"""Tests for analytics service formatters and question router.

These tests use static data and do not require a database connection.
"""

import pytest

from core.analytics import AnalyticsService


def test_format_gas_answer() -> None:
    data = {
        "total_consumed_bar": 145.0,
        "avg_per_session_bar": 14.5,
        "cylinder_changes": 2,
        "consumption_records": 5,
        "cylinders_used": ["AG-041", "AG-042"],
        "sessions_analyzed": 10,
    }
    answer = AnalyticsService._format_gas_answer(data, 10)
    assert "145.0" in answer
    assert "14.5" in answer
    assert "AG-041" in answer


def test_format_gas_answer_no_records() -> None:
    data = {
        "total_consumed_bar": 0.0,
        "avg_per_session_bar": 0.0,
        "cylinder_changes": 0,
        "consumption_records": 0,
        "cylinders_used": [],
        "sessions_analyzed": 10,
    }
    answer = AnalyticsService._format_gas_answer(data, 10)
    assert "Нет записей расхода" in answer


def test_format_powder_answer() -> None:
    data = {
        "total_consumed_kg": 25.5,
        "avg_per_session_kg": 2.55,
        "batches_used": ["AL-001", "AL-002"],
        "reuse_events": 3,
        "max_reuse_cycle": 5,
        "sessions_analyzed": 10,
    }
    answer = AnalyticsService._format_powder_answer(data, 10)
    assert "25.5" in answer
    assert "AL-001" in answer
    assert "5" in answer


def test_format_quality_answer() -> None:
    data = {
        "total_outcomes": 10,
        "accepted": 7,
        "rejected": 2,
        "warnings": 1,
        "accepted_pct": 70.0,
        "rejected_pct": 20.0,
        "defect_types": {"porosity": 1, "crack": 1},
        "sessions_analyzed": 10,
    }
    answer = AnalyticsService._format_quality_answer(data, 10)
    assert "70.0%" in answer
    assert "20.0%" in answer
    assert "porosity" in answer


def test_format_sessions_answer_empty() -> None:
    assert AnalyticsService._format_sessions_answer([]) == "Нет данных о сессиях."


def test_format_sessions_answer_with_data() -> None:
    sessions = [
        {
            "session_id": "s1",
            "start_ts": "2026-04-27T10:00:00",
            "end_ts": "2026-04-27T14:00:00",
            "classification": "REAL_PRINT",
            "material": "AlSi10Mg",
            "duration_sec": 14400,
        }
    ]
    answer = AnalyticsService._format_sessions_answer(sessions)
    assert "s1" in answer
    assert "4.0" in answer
    assert "AlSi10Mg" in answer


def test_answer_question_routes_to_gas() -> None:
    svc = AnalyticsService()
    try:
        result = svc.answer_question("Сколько аргона ушло?")
        assert result["topic"] == "gas"
        assert result["direct_answer"] is not None
    finally:
        svc.close()


def test_answer_question_routes_to_powder() -> None:
    svc = AnalyticsService()
    try:
        result = svc.answer_question("Сколько порошка потрачено?")
        assert result["topic"] == "powder"
        assert result["direct_answer"] is not None
    finally:
        svc.close()


def test_answer_question_routes_to_quality() -> None:
    svc = AnalyticsService()
    try:
        result = svc.answer_question("Какой процент брака?")
        assert result["topic"] == "quality"
        assert result["direct_answer"] is not None
    finally:
        svc.close()


def test_answer_question_routes_to_sessions() -> None:
    svc = AnalyticsService()
    try:
        result = svc.answer_question("Покажи последние 5 печатей")
        assert result["topic"] == "sessions"
        assert result["direct_answer"] is not None
    finally:
        svc.close()


def test_answer_question_unknown_returns_all() -> None:
    svc = AnalyticsService()
    try:
        result = svc.answer_question("Расскажи всё")
        assert result["topic"] == "unknown"
        assert "gas" in result["data"]
        assert "powder" in result["data"]
        assert "quality" in result["data"]
    finally:
        svc.close()
