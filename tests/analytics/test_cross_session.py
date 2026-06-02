"""Tests for analytics.cross_session — pattern detection across sessions."""
import pytest
from analytics.cross_session import (
    detect_signal_trends,
    detect_session_anomalies,
    correlate_events_with_signals,
    run_cross_session_analysis,
)


def _make_session(sid: str, ts: str, so1: float, st5: float = 168.0) -> dict:
    return {
        "session_id": sid,
        "start_ts": ts,
        "classification": "REAL_PRINT",
        "signal_stats": {
            "SO1": {"mean": so1, "std": 0.02, "p95": so1 + 0.1, "min": so1 - 0.05,
                    "max": so1 + 0.5, "p05": so1 - 0.02, "n": 150, "trend_slope": 0.0,
                    "group": "oxygen"},
            "ST5": {"mean": st5, "std": 2.0, "p95": st5 + 5, "min": st5 - 10,
                    "max": st5 + 8, "p05": st5 - 8, "n": 150, "trend_slope": 0.0,
                    "group": "temperature"},
        },
    }


# ── Trend detector ──────────────────────────────────────────────────────────

def test_detect_rising_o2_trend():
    # O₂ rising 10% per session → should be detected.
    sessions = [_make_session(f"s{i}", f"2026-01-0{i+1}", so1=0.10 + i * 0.05) for i in range(5)]
    findings = detect_signal_trends(sessions)
    so1_findings = [f for f in findings if f["signal"] == "SO1"]
    assert len(so1_findings) > 0
    assert so1_findings[0]["direction"] == "increasing"


def test_stable_signal_not_flagged():
    # Constant O₂ → no trend.
    sessions = [_make_session(f"s{i}", f"2026-01-0{i+1}", so1=0.10) for i in range(5)]
    findings = detect_signal_trends(sessions)
    assert findings == []


def test_needs_minimum_sessions():
    sessions = [_make_session(f"s{i}", f"2026-01-0{i+1}", so1=0.10 + i * 0.05) for i in range(2)]
    assert detect_signal_trends(sessions) == []


def test_trend_direction_decreasing():
    sessions = [_make_session(f"s{i}", f"2026-01-0{i+1}", so1=0.50 - i * 0.05) for i in range(5)]
    findings = detect_signal_trends(sessions)
    so1_findings = [f for f in findings if f["signal"] == "SO1"]
    assert so1_findings[0]["direction"] == "decreasing"


# ── Session anomaly detector ────────────────────────────────────────────────

def test_outlier_session_detected():
    # 5 normal sessions + 1 with massively elevated O₂.
    sessions = [_make_session(f"s{i}", f"2026-01-0{i+1}", so1=0.10) for i in range(5)]
    sessions.append(_make_session("s_bad", "2026-01-06", so1=5.0))  # huge spike
    findings = detect_session_anomalies(sessions)
    bad = [f for f in findings if f["session_id"] == "s_bad" and f["signal"] == "SO1"]
    assert len(bad) > 0
    assert bad[0]["direction"] == "high"
    assert bad[0]["z_score"] > 2.5


def test_no_anomalies_in_uniform_data():
    sessions = [_make_session(f"s{i}", f"2026-01-0{i+1}", so1=0.10) for i in range(6)]
    findings = detect_session_anomalies(sessions)
    assert findings == []


def test_needs_at_least_3_sessions():
    sessions = [_make_session(f"s{i}", f"2026-01-0{i+1}", so1=0.10) for i in range(2)]
    assert detect_session_anomalies(sessions) == []


# ── Before/after correlator ─────────────────────────────────────────────────

def test_before_after_detects_improvement():
    # O₂ high before seal replacement, low after.
    sessions = [
        _make_session("s1", "2026-01-01", so1=0.80),
        _make_session("s2", "2026-01-02", so1=0.85),
        _make_session("s3", "2026-01-03", so1=0.90),
        _make_session("s4", "2026-01-06", so1=0.10),
        _make_session("s5", "2026-01-07", so1=0.11),
    ]
    events = [{"event_type": "seal_replaced", "timestamp": "2026-01-05"}]
    findings = correlate_events_with_signals(sessions, events)
    seal = [f for f in findings if f["event_type"] == "seal_replaced" and f["signal"] == "SO1"]
    assert len(seal) > 0
    assert seal[0]["delta_pct"] < -50   # significant drop


def test_before_after_no_change_not_flagged():
    sessions = [
        _make_session("s1", "2026-01-01", so1=0.10),
        _make_session("s2", "2026-01-02", so1=0.10),
        _make_session("s3", "2026-01-06", so1=0.10),
        _make_session("s4", "2026-01-07", so1=0.10),
    ]
    events = [{"event_type": "seal_replaced", "timestamp": "2026-01-05"}]
    findings = correlate_events_with_signals(sessions, events)
    assert findings == []


# ── Full pipeline ────────────────────────────────────────────────────────────

def test_run_cross_session_analysis_returns_dict():
    sessions = [_make_session(f"s{i}", f"2026-01-0{i+1}", so1=0.10 + i * 0.05) for i in range(5)]
    result = run_cross_session_analysis(sessions)
    assert "trends" in result
    assert "anomalies" in result
    assert "before_after" in result
    assert "summary" in result
    assert result["n_sessions_analyzed"] == 5


def test_run_cross_session_with_no_sessions():
    result = run_cross_session_analysis([])
    assert result["n_sessions_analyzed"] == 0
    assert "отклонений не обнаружено" in result["summary"]
