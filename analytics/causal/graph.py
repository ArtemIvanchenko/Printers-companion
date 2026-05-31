from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import networkx as nx


@dataclass
class CausalDataQuality:
    sample_size: int = 0
    number_of_failures: int = 0
    number_of_successes: int = 0
    operator_context_coverage: float = 0.0
    quality_outcome_coverage: float = 0.0
    material_context_coverage: float = 0.0
    maintenance_context_coverage: float = 0.0
    gas_context_coverage: float = 0.0
    missing_data_penalty: float = 0.0
    counterexample_count: int = 0
    data_quality_score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class CandidateHypothesis:
    title: str
    relationship: str
    confidence: float
    summary: str
    supporting_evidence: list[dict[str, Any]] = field(default_factory=list)
    contradictions: list[dict[str, Any]] = field(default_factory=list)
    causal_data_quality: CausalDataQuality = field(default_factory=CausalDataQuality)


def build_temporal_dependency_graph(items: list[dict[str, Any]], near_seconds: int = 3600) -> nx.DiGraph:
    graph = nx.DiGraph()
    sorted_items = sorted(items, key=lambda item: item.get("ts") or datetime.min)
    for item in sorted_items:
        graph.add_node(item["id"], **item)
    for source, target in zip(sorted_items, sorted_items[1:], strict=False):
        if source.get("ts") and target.get("ts"):
            delta = (target["ts"] - source["ts"]).total_seconds()
            relationship = "temporally_near" if delta <= near_seconds else "precedes"
            graph.add_edge(source["id"], target["id"], relationship=relationship, delta_sec=delta)
    return graph


def score_hypothesis(
    sample_size: int,
    effect_size: float,
    data_quality: CausalDataQuality,
    counterexamples: int = 0,
) -> tuple[str, float]:
    if sample_size < 3:
        return "observation", min(0.25, data_quality.data_quality_score)
    base = min(abs(effect_size), 1.0) * 0.45 + data_quality.data_quality_score * 0.45
    penalty = min(counterexamples * 0.08 + data_quality.missing_data_penalty, 0.5)
    confidence = max(0.0, min(base - penalty, 0.95))
    if sample_size < 10:
        return "weak_hypothesis", min(confidence, 0.55)
    return "candidate_insight", confidence


def generate_candidate_hypotheses(graph: nx.DiGraph, data_quality: CausalDataQuality) -> list[CandidateHypothesis]:
    hypotheses: list[CandidateHypothesis] = []
    for source, target, edge in graph.edges(data=True):
        source_data = graph.nodes[source]
        target_data = graph.nodes[target]
        if source_data.get("kind") == "operator_event" and target_data.get("kind") == "anomaly":
            status, confidence = score_hypothesis(
                sample_size=data_quality.sample_size,
                effect_size=0.3,
                data_quality=data_quality,
                counterexamples=data_quality.counterexample_count,
            )
            hypotheses.append(
                CandidateHypothesis(
                    title="Operator context precedes anomaly",
                    relationship="temporally_near" if edge.get("relationship") == "temporally_near" else "precedes",
                    confidence=confidence,
                    summary=(
                        f"{status}: operator event {source_data.get('event_type')} occurred before "
                        f"anomaly {target_data.get('anomaly_type')}. This is not proof of causation."
                    ),
                    supporting_evidence=[{"source": source, "target": target, "edge": edge}],
                    causal_data_quality=data_quality,
                )
            )
    return hypotheses

