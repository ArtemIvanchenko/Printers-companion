from typing import Any

from analytics.causal.graph import CausalDataQuality, score_hypothesis


def generate_hypotheses_from_patterns(patterns: list[dict[str, Any]], sample_size: int) -> list[dict[str, Any]]:
    data_quality = CausalDataQuality(
        sample_size=sample_size,
        data_quality_score=0.45 if sample_size < 10 else 0.7,
        missing_data_penalty=0.15,
    )
    hypotheses = []
    for pattern in patterns:
        status, confidence = score_hypothesis(
            sample_size=sample_size,
            effect_size=min(pattern["count"] / max(sample_size, 1), 1.0),
            data_quality=data_quality,
        )
        hypotheses.append(
            {
                "title": f"Recurring pattern: {pattern['pattern']}",
                "status": status,
                "confidence": confidence,
                "supporting_sessions": pattern["supporting_sessions"],
                "causal_data_quality": data_quality.to_dict(),
            }
        )
    return hypotheses

