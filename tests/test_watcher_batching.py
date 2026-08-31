from pathlib import Path

from worker import watcher
from worker.watcher import CandidateTracker, import_batch_for, scan_existing_candidates


def test_loose_logs_are_grouped_as_watched_parent(tmp_path):
    log = tmp_path / "01.01.2026_time.log"
    log.write_text("x")

    assert import_batch_for(log, tmp_path) == tmp_path


def test_folder_and_zip_remain_independent_batches(tmp_path):
    folder = tmp_path / "printer_export"
    folder.mkdir()
    archive = tmp_path / "printer_export.zip"
    archive.write_bytes(b"x")

    assert import_batch_for(folder, tmp_path) == folder
    assert import_batch_for(archive, tmp_path) == archive
    assert import_batch_for(Path(tmp_path / "readme.txt"), tmp_path) is None


def _capture_notifications(monkeypatch):
    calls: list[Path] = []

    def notify(path: Path) -> dict:
        calls.append(path)
        return {"job": {"import_job_id": "test-job"}}

    monkeypatch.setattr(watcher, "notify_import_detected", notify)
    monkeypatch.setattr(watcher, "auto_confirm_import", lambda response: None)
    return calls


def test_loose_log_batch_is_reported_again_after_contents_change(tmp_path, monkeypatch):
    calls = _capture_notifications(monkeypatch)
    tracker = CandidateTracker()
    (tmp_path / "time.log").write_text("first")

    scan_existing_candidates(tmp_path, tracker)
    scan_existing_candidates(tmp_path, tracker)
    assert calls == [tmp_path]

    (tmp_path / "state.log").write_text("second")
    scan_existing_candidates(tmp_path, tracker)
    assert calls == [tmp_path, tmp_path]


def test_unrelated_zip_does_not_change_loose_log_batch(tmp_path, monkeypatch):
    calls = _capture_notifications(monkeypatch)
    tracker = CandidateTracker()
    (tmp_path / "time.log").write_text("stable loose batch")

    scan_existing_candidates(tmp_path, tracker)
    archive = tmp_path / "separate-export.zip"
    archive.write_bytes(b"independent batch")
    scan_existing_candidates(tmp_path, tracker)

    assert calls.count(tmp_path) == 1
    assert calls.count(archive) == 1


def test_replaced_zip_at_same_path_is_reported_again(tmp_path, monkeypatch):
    calls = _capture_notifications(monkeypatch)
    tracker = CandidateTracker()
    archive = tmp_path / "export.zip"
    archive.write_bytes(b"one")

    scan_existing_candidates(tmp_path, tracker)
    scan_existing_candidates(tmp_path, tracker)
    assert calls == [archive]

    archive.write_bytes(b"replacement-is-larger")
    scan_existing_candidates(tmp_path, tracker)
    assert calls == [archive, archive]


def test_changed_folder_at_same_path_is_reported_again(tmp_path, monkeypatch):
    calls = _capture_notifications(monkeypatch)
    tracker = CandidateTracker()
    folder = tmp_path / "export"
    folder.mkdir()
    log = folder / "time.log"
    log.write_text("one")

    scan_existing_candidates(tmp_path, tracker)
    scan_existing_candidates(tmp_path, tracker)
    assert calls == [folder]

    log.write_text("replacement-is-larger")
    scan_existing_candidates(tmp_path, tracker)
    assert calls == [folder, folder]


def test_failed_notification_releases_exact_signature_for_retry(tmp_path, monkeypatch):
    tracker = CandidateTracker()
    archive = tmp_path / "export.zip"
    archive.write_bytes(b"one")
    attempts = 0

    def notify(path: Path) -> dict:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError("temporary API outage")
        return {"job": {"import_job_id": "test-job"}}

    monkeypatch.setattr(watcher, "notify_import_detected", notify)
    monkeypatch.setattr(watcher, "auto_confirm_import", lambda response: None)

    scan_existing_candidates(tmp_path, tracker)
    scan_existing_candidates(tmp_path, tracker)
    assert attempts == 2
