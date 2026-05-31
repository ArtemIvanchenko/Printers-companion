from typing import Any


def build_missing_context_questions(session_id: str, missing_fields: list[str]) -> list[dict[str, Any]]:
    return [
        {
            "session_id": session_id,
            "field": field,
            "question": f"Please confirm {field} for session {session_id}.",
        }
        for field in missing_fields
    ]

