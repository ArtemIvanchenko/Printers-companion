"""Calibration-critical logs must survive not being on this machine's disk.

Against a shared database (the planned NAS setup) an operator sees every
print, but only has the log files for the ones they imported themselves. Every
calibration reads logs through ``rehydrate=True``, and each returns None when
the file is missing — so the accuracy loop silently falls back to wall-clock
time, which carries operator pauses (18 h of 47.6 on one real build). Nothing
errors; the numbers just quietly get worse.

Mirroring only time_log keeps this cheap: 3 MB across this shop's 19 real
prints, against 10 GB for the full log set.
"""
from datetime import datetime, timezone
from pathlib import Path

import pytest

from domain.enums.common import DataQualityStatus, SourceFileFamily
from domain.schemas.parsing import FileClassification
from domain.services.ingestion import IngestedFile
from storage.repositories import runtime as runtime_mod

_TIME_LOG = "\n".join(
    f"OLD_STATS: {n} | 9250 | 30000 | 39250 |" for n in range(1, 6)
) + "\n"


def _file(path: Path, family=SourceFileFamily.time_log) -> IngestedFile:
    return IngestedFile(
        path=str(path),
        relative_path=path.name,
        classification=FileClassification(
            path=str(path), file_name=path.name, family=family,
            role="secondary", confidence=1.0,
        ),
        checksum="x", size_bytes=path.stat().st_size if path.exists() else 1,
        data_quality_status=DataQualityStatus.ok,
        mtime=datetime.now(timezone.utc), parse_result=None,
    )


class _FakeStore:
    """Stands in for MinIO: same surface, a dict for a backend."""

    def __init__(self, available=True):
        self.objects: dict[tuple[str, str], bytes] = {}
        self._available = available

        class _S:
            minio_bucket_raw = "raw-logs"
        self.settings = _S()

    def is_available(self):
        return self._available

    def put_file(self, bucket, name, path):
        self.objects[(bucket, name)] = Path(path).read_bytes()
        return f"s3://{bucket}/{name}"

    def get_bytes(self, bucket, name):
        return self.objects.get((bucket, name))


@pytest.fixture
def store(monkeypatch):
    fake = _FakeStore()
    monkeypatch.setattr(
        "storage.object_store.minio_client.ObjectStore", lambda *a, **k: fake,
    )
    return fake


class TestMirroring:
    def test_time_log_is_mirrored(self, tmp_path, store):
        log = tmp_path / "23.03.2026_time.log"
        log.write_text(_TIME_LOG, encoding="utf-8")

        n = runtime_mod.mirror_logs_to_object_store("s1", [_file(log)])

        assert n == 1
        assert store.objects[("raw-logs", "s1/23.03.2026_time.log")] == _TIME_LOG.encode()

    def test_bulky_families_are_not_mirrored(self, tmp_path, store):
        """sensors is 1.4 GB and stateFlow 9 GB across the same prints."""
        sensors = tmp_path / "23.03.2026_sensors.log"
        sensors.write_text("Time|LIR|\n", encoding="utf-8")

        assert runtime_mod.mirror_logs_to_object_store(
            "s1", [_file(sensors, SourceFileFamily.sensors_log)],
        ) == 0
        assert store.objects == {}

    def test_object_store_down_does_not_break_import(self, tmp_path, monkeypatch):
        log = tmp_path / "t_time.log"
        log.write_text(_TIME_LOG, encoding="utf-8")

        def _boom(*a, **k):
            raise RuntimeError("minio unreachable")

        monkeypatch.setattr("storage.object_store.minio_client.ObjectStore", _boom)
        # The on-disk copy is still primary; a mirror failure must not propagate.
        assert runtime_mod.mirror_logs_to_object_store("s1", [_file(log)]) == 0

    def test_missing_file_is_skipped(self, tmp_path, store):
        ghost = _file(tmp_path / "gone_time.log")
        assert runtime_mod.mirror_logs_to_object_store("s1", [ghost]) == 0


class TestRehydrateFallback:
    def test_reads_from_disk_when_present(self, tmp_path, store):
        log = tmp_path / "23.03.2026_time.log"
        log.write_text(_TIME_LOG, encoding="utf-8")

        files = runtime_mod._rehydrate_parse_results([_file(log)], "s1")
        assert files[0].parse_result is not None

    def test_falls_back_to_the_mirror_when_the_file_is_elsewhere(self, tmp_path, store):
        """The case this whole mechanism exists for: another operator's import."""
        log = tmp_path / "23.03.2026_time.log"
        log.write_text(_TIME_LOG, encoding="utf-8")
        runtime_mod.mirror_logs_to_object_store("s1", [_file(log)])

        stale = _file(log)
        log.unlink()  # this machine never had it
        assert not Path(stale.path).exists()

        files = runtime_mod._rehydrate_parse_results([stale], "s1")
        assert files[0].parse_result is not None
        events = [
            e for e in files[0].parse_result.events
            if getattr(e, "event_type", None) == "layer_timing_summary"
        ]
        assert len(events) == 5

    def test_no_session_id_means_no_fallback(self, tmp_path, store):
        """Callers that cannot name the session get the old behaviour."""
        log = tmp_path / "23.03.2026_time.log"
        log.write_text(_TIME_LOG, encoding="utf-8")
        runtime_mod.mirror_logs_to_object_store("s1", [_file(log)])

        stale = _file(log)
        log.unlink()
        assert runtime_mod._rehydrate_parse_results([stale])[0].parse_result is None

    def test_absent_from_both_stays_slim(self, tmp_path, store):
        ghost = _file(tmp_path / "never_time.log")
        assert runtime_mod._rehydrate_parse_results([ghost], "s1")[0].parse_result is None
