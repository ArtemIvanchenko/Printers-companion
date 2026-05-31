from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class IncrementalTracker:
    last_run_at: datetime | None = None
    changed_session_ids: set[str] = field(default_factory=set)

    def mark_changed(self, session_id: str) -> None:
        self.changed_session_ids.add(session_id)

    def complete(self) -> None:
        self.last_run_at = datetime.now(timezone.utc)
        self.changed_session_ids.clear()

