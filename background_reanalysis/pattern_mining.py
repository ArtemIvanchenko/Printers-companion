from collections import Counter
from typing import Any


def mine_repeated_patterns(session_features: list[dict[str, Any]], min_count: int = 3) -> list[dict[str, Any]]:
    counter = Counter()
    examples: dict[str, list[str]] = {}
    for features in session_features:
        for key, value in features.items():
            if key.endswith("_count") and isinstance(value, int) and value > 0:
                pattern = f"{key}:{value}"
                counter[pattern] += 1
                examples.setdefault(pattern, []).append(features.get("session_id", "unknown"))
    return [
        {"pattern": pattern, "count": count, "supporting_sessions": examples.get(pattern, [])}
        for pattern, count in counter.items()
        if count >= min_count
    ]

