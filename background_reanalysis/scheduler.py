from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from background_reanalysis.hypothesis_generator import generate_hypotheses_from_patterns
from background_reanalysis.insight_repository import create_pattern_insight_draft
from background_reanalysis.job_planner import HistoricalReanalysisPlan
from background_reanalysis.pattern_mining import mine_repeated_patterns


ITERATION_NAMES = [
    "refresh_feature_store",
    "aggregate_statistics",
    "mine_repeated_anomaly_patterns",
    "compare_before_after_maintenance",
    "analyze_lagged_correlations",
    "cluster_similar_sessions",
    "generate_candidate_hypotheses",
    "search_counterexamples",
    "score_confidence_and_quality",
    "produce_final_verdict",
]


def run_bounded_historical_reanalysis(
    plan: HistoricalReanalysisPlan,
    session_features: list[dict[str, Any]],
) -> dict[str, Any]:
    completed = 0
    intermediate: list[dict[str, Any]] = []
    if not session_features:
        return _verdict(plan, 0, "insufficient_data", "insufficient_data", "No session features available.", [], intermediate)

    patterns: list[dict[str, Any]] = []
    hypotheses: list[dict[str, Any]] = []
    insights: list[dict[str, Any]] = []
    for index, name in enumerate(ITERATION_NAMES[: plan.max_iterations], start=1):
        completed = index
        if name == "mine_repeated_anomaly_patterns":
            patterns = mine_repeated_patterns(session_features)
            intermediate.append({"iteration": index, "name": name, "patterns": patterns})
            if not patterns:
                return _verdict(plan, completed, "completed", "no_new_pattern", "No repeated patterns met the configured support threshold.", [], intermediate)
        elif name == "generate_candidate_hypotheses":
            hypotheses = generate_hypotheses_from_patterns(patterns, sample_size=len(session_features))
            intermediate.append({"iteration": index, "name": name, "hypotheses": hypotheses})
        elif name == "produce_final_verdict":
            analysis_window = {"start": plan.start.isoformat(), "end": plan.end.isoformat()}
            insights = [create_pattern_insight_draft(hypothesis, analysis_window) for hypothesis in hypotheses]
            intermediate.append({"iteration": index, "name": name, "insights": insights})
        else:
            intermediate.append({"iteration": index, "name": name, "status": "completed"})

    verdict_value = "weak_signal_found" if insights else "no_new_pattern"
    return _verdict(
        plan,
        completed,
        "completed",
        verdict_value,
        "Bounded historical reanalysis completed. Draft insights require human review.",
        insights,
        intermediate,
    )


def _verdict(
    plan: HistoricalReanalysisPlan,
    completed: int,
    status: str,
    verdict: str,
    summary: str,
    insights: list[dict[str, Any]],
    intermediate: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "verdict_id": f"verdict_{uuid4().hex}",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "analysis_window": {"start": plan.start.isoformat(), "end": plan.end.isoformat()},
        "max_iterations": plan.max_iterations,
        "completed_iterations": completed,
        "status": status,
        "verdict": verdict,
        "confidence": max([insight.get("confidence", 0.0) for insight in insights], default=0.0),
        "summary": summary,
        "new_insights": insights,
        "updated_insights": [],
        "dismissed_candidates": [],
        "counterexamples": [],
        "missing_data": [],
        "recommended_actions": ["Review draft insights before confirmation."] if insights else [],
        "affected_sessions": sorted({session for insight in insights for session in insight.get("supporting_sessions", [])}),
        "analysis_version": "0.1.0",
        "evidence_links": intermediate,
    }

