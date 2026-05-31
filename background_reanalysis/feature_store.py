from typing import Any


class FeatureStore:
    def __init__(self) -> None:
        self._features: dict[str, dict[str, Any]] = {}

    def upsert_session_features(self, session_id: str, features: dict[str, Any]) -> None:
        self._features[session_id] = features

    def list_features(self) -> list[dict[str, Any]]:
        return [{"session_id": session_id, **features} for session_id, features in self._features.items()]

