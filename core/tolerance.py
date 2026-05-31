"""Tolerance checker — learns acceptable ranges from operator confirmations."""

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from domain.models.entities import ToleranceRule

logger = logging.getLogger(__name__)


def learn_from_session(
    db: Session,
    session_id: str,
    features: dict[str, Any],
    confirmed_by: str = "operator",
) -> list[ToleranceRule]:
    """Create or update tolerance rules based on an approved session."""
    created_rules = []
    for key, value in features.items():
        if not isinstance(value, (int, float)):
            continue

        value = float(value)
        rule = db.scalars(
            select(ToleranceRule).where(
                ToleranceRule.feature_name == key,
                ToleranceRule.is_active == True,
            )
        ).first()

        if rule:
            # Expand range if new value is outside current bounds
            if rule.min_value is None or value < rule.min_value:
                rule.min_value = value
            if rule.max_value is None or value > rule.max_value:
                rule.max_value = value
            rule.session_id_reference = session_id
            logger.info("Updated tolerance rule for %s: [%.2f, %.2f]", key, rule.min_value, rule.max_value)
        else:
            # Create new rule with small margin (+/- 5%)
            margin = max(value * 0.05, 1.0)
            new_rule = ToleranceRule(
                feature_name=key,
                min_value=value - margin,
                max_value=value + margin,
                confirmed_by=confirmed_by,
                session_id_reference=session_id,
            )
            db.add(new_rule)
            created_rules.append(new_rule)
            logger.info("Created tolerance rule for %s: [%.2f, %.2f]", key, new_rule.min_value, new_rule.max_value)

    db.commit()
    return created_rules


def check_session(
    db: Session,
    features: dict[str, Any],
) -> list[dict[str, Any]]:
    """Check session features against active tolerance rules.
    
    Returns list of violations. Empty list means session is within norms.
    """
    violations = []
    rules = db.scalars(select(ToleranceRule).where(ToleranceRule.is_active == True)).all()
    rule_map = {r.feature_name: r for r in rules}

    for key, value in features.items():
        if not isinstance(value, (int, float)):
            continue

        value = float(value)
        rule = rule_map.get(key)
        if not rule:
            continue

        if rule.min_value is not None and value < rule.min_value:
            violations.append({
                "feature": key,
                "value": value,
                "expected_min": rule.min_value,
                "reason": "below_learned_tolerance",
            })
        elif rule.max_value is not None and value > rule.max_value:
            violations.append({
                "feature": key,
                "value": value,
                "expected_max": rule.max_value,
                "reason": "above_learned_tolerance",
            })

    return violations
