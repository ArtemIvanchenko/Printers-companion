"""Unit tests for the shared session-import core (domain/services/session_import).

This is the single source of truth the startup scan, upload rescan, /sessions
ingest and confirmed-import worker now share, so its idempotency and the
parse_result-stripping are pinned here.
"""
from pathlib import Path

import domain.services.session_import as si


class _FakeGroup:
    def __init__(self, group_id: str):
        self.group_id = group_id
        self.files = []
        self.start_ts = None
        self.end_ts = None
        self.confidence = 0.5


class _FakeRepo:
    def __init__(self, existing):
        self._existing = set(existing)
        self.saved: list[tuple[str, dict]] = []

    def list_session_payloads(self):
        return [(sid, {}) for sid in self._existing]

    def save_session_payload(self, session_id, payload):
        self.saved.append((session_id, payload))


def test_import_new_sessions_skips_already_stored(monkeypatch):
    groups = [_FakeGroup("session_A"), _FakeGroup("session_B")]
    monkeypatch.setattr(si, "scan_and_group", lambda folder: groups)
    monkeypatch.setattr(si, "overview_for_group", lambda g: {"group_id": g.group_id})

    repo = _FakeRepo(existing={"session_A"})
    stats = si.import_new_sessions(Path("/whatever"), repo)

    assert stats == {"found": 2, "imported": 1}
    assert [sid for sid, _ in repo.saved] == ["session_B"]


def test_import_new_sessions_no_groups(monkeypatch):
    monkeypatch.setattr(si, "scan_and_group", lambda folder: [])
    repo = _FakeRepo(existing=set())
    assert si.import_new_sessions(Path("/x"), repo) == {"found": 0, "imported": 0}
    assert repo.saved == []


def test_slim_session_payload_strips_parse_result():
    class _File:
        def model_dump(self, mode=None, exclude=None):
            return {"path": "x.log", "_exclude": exclude}

    class _Group:
        files = [_File()]

    payload = si.slim_session_payload(_Group(), {"k": "v"})
    assert payload["group"] == {"k": "v"}
    # Events must be excluded so the stored row stays small.
    assert payload["files"][0]["_exclude"] == {"parse_result"}
