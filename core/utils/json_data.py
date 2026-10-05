"""Normalize non-finite measurements before persisting JSON."""

import math
from typing import Any


def sanitize_json(value: Any) -> Any:
    """Replace NaN/Infinity with unknown, without inventing a zero measurement."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: sanitize_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_json(item) for item in value]
    return value
