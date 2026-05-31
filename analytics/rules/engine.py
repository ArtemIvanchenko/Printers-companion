from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class RuleResult:
    rule_id: str
    matched: bool
    severity: str = "info"
    confidence: float = 0.0
    message: str = ""
    evidence: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Rule:
    rule_id: str
    description: str
    evaluator: Callable[[dict[str, Any]], RuleResult]


class RuleEngine:
    def __init__(self, rules: list[Rule] | None = None) -> None:
        self.rules = rules or []

    def register(self, rule: Rule) -> None:
        self.rules.append(rule)

    def run(self, facts: dict[str, Any]) -> list[RuleResult]:
        return [rule.evaluator(facts) for rule in self.rules]


def default_rule_engine() -> RuleEngine:
    engine = RuleEngine()

    def restart_chain(facts: dict[str, Any]) -> RuleResult:
        count = int(facts.get("restart_attempt_count") or 0)
        return RuleResult(
            rule_id="restart_chain_detected",
            matched=count >= 2,
            severity="warning" if count >= 2 else "info",
            confidence=min(0.4 + count * 0.1, 0.9) if count >= 2 else 0.0,
            message=f"{count} restart attempts detected." if count >= 2 else "No restart chain.",
        )

    def missing_context(facts: dict[str, Any]) -> RuleResult:
        missing = [field for field in ("material", "powder_batch", "gas_cylinder_id") if not facts.get(field)]
        return RuleResult(
            rule_id="missing_production_context",
            matched=bool(missing),
            severity="warning" if missing else "info",
            confidence=1.0 if missing else 0.0,
            message=f"Missing production context: {', '.join(missing)}" if missing else "Production context present.",
            evidence=[{"missing": missing}],
        )

    engine.register(Rule("restart_chain_detected", "Detect repeated restart attempts.", restart_chain))
    engine.register(Rule("missing_production_context", "Detect missing context for analysis.", missing_context))
    return engine

