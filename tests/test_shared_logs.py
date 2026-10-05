"""Calibration-critical logs have a compact session-addressable NAS copy.

Production calibration uses shared LayerSnapshots, not raw re-parsing. The
owner-local legacy repair path can still restore a missing local time log from
its compact NAS mirror. The complete raw import is archived separately before
analysis; large sensor and state files are not copied into this extra mirror.
"""
from datetime import datetime, timezone
import hashlib
from pathlib import Path

import pytest

from domain.enums.common import DataQualityStatus, SourceFileFamily
from domain.schemas.parsing import FileClassification
from domain.services.ingestion import IngestedFile
from domain.services import session_sources as sources_mod

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
        checksum=hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "0" * 64,
        size_bytes=path.stat().st_size if path.exists() else 1,
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

    def put_file_verified(self, bucket, name, path, *, expected_sha256, expected_size):
        data = Path(path).read_bytes()
        assert hashlib.sha256(data).hexdigest() == expected_sha256
        assert len(data) == expected_size
        return self.put_file(bucket, name, path)


@pytest.fixture
def store(monkeypatch):
    fake = _FakeStore()
    monkeypatch.setattr(
        "storage.object_store.minio_client.ObjectStore", lambda *a, **k: fake,
    )
    return fake


class TestMirroring:
    def test_immutable_mirrors_do_not_replace_each_other(self, tmp_path, store):
        path = tmp_path / "t_time.log"
        path.write_text(_TIME_LOG, encoding="utf-8")
        first = _file(path)
        first.checksum = hashlib.sha256(path.read_bytes()).hexdigest()
        assert sources_mod.mirror_logs_to_object_store("s1", [first], immutable=True) == 1
        uri = first.metadata["shared_log_uri"]
        path.write_text(_TIME_LOG + "OLD_STATS: 6 | 9250 | 30000 | 39250 |\n", encoding="utf-8")
        second = _file(path)
        second.checksum = hashlib.sha256(path.read_bytes()).hexdigest()
        assert sources_mod.mirror_logs_to_object_store("s1", [second], immutable=True) == 1
        assert second.metadata["shared_log_uri"] != uri
        old_copy = sources_mod.fetch_shared_log("s1", path.name, object_uri=uri)
        new_copy = sources_mod.fetch_shared_log("s1", path.name, object_uri=second.metadata["shared_log_uri"])
        assert old_copy != new_copy
        assert old_copy.read_bytes() == _TIME_LOG.encode()
        assert new_copy.read_bytes() == path.read_bytes()

    def test_corrupt_or_foreign_immutable_mirror_is_not_read(self, store):
        checksum = "a" * 64
        name = f"s1/sha256/{checksum}/t_time.log"
        store.objects[("raw-logs", name)] = b"wrong content"
        with pytest.raises(ValueError, match="нужен повторный импорт"):
            sources_mod.fetch_shared_log("s1", "t_time.log", object_uri=f"s3://raw-logs/{name}")
        assert sources_mod.fetch_shared_log("s2", "t_time.log", object_uri=f"s3://raw-logs/{name}") is None

    def test_time_log_is_mirrored(self, tmp_path, store):
        log = tmp_path / "23.03.2026_time.log"
        log.write_text(_TIME_LOG, encoding="utf-8")

        n = sources_mod.mirror_logs_to_object_store("s1", [_file(log)])

        assert n == 1
        assert store.objects[("raw-logs", "s1/23.03.2026_time.log")] == _TIME_LOG.encode()

    def test_bulky_families_are_not_mirrored(self, tmp_path, store):
        """sensors is 1.4 GB and stateFlow 9 GB across the same prints."""
        sensors = tmp_path / "23.03.2026_sensors.log"
        sensors.write_text("Time|LIR|\n", encoding="utf-8")

        assert sources_mod.mirror_logs_to_object_store(
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
        assert sources_mod.mirror_logs_to_object_store("s1", [_file(log)]) == 0

    def test_missing_file_is_skipped(self, tmp_path, store):
        ghost = _file(tmp_path / "gone_time.log")
        assert sources_mod.mirror_logs_to_object_store("s1", [ghost]) == 0


class TestRehydrateFallback:
    def test_reads_from_disk_when_present(self, tmp_path, store):
        log = tmp_path / "23.03.2026_time.log"
        log.write_text(_TIME_LOG, encoding="utf-8")

        files = sources_mod.rehydrate_parse_results([_file(log)], "s1")
        assert files[0].parse_result is not None

    def test_falls_back_to_the_mirror_when_the_file_is_elsewhere(self, tmp_path, store):
        """An owner-local repair can restore a missing original from the NAS."""
        log = tmp_path / "23.03.2026_time.log"
        log.write_text(_TIME_LOG, encoding="utf-8")
        sources_mod.mirror_logs_to_object_store("s1", [_file(log)])

        stale = _file(log)
        log.unlink()  # this machine never had it
        assert not Path(stale.path).exists()

        files = sources_mod.rehydrate_parse_results([stale], "s1")
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
        sources_mod.mirror_logs_to_object_store("s1", [_file(log)])

        stale = _file(log)
        log.unlink()
        assert sources_mod.rehydrate_parse_results([stale])[0].parse_result is None

    def test_absent_from_both_stays_slim(self, tmp_path, store):
        ghost = _file(tmp_path / "never_time.log")
        assert sources_mod.rehydrate_parse_results([ghost], "s1")[0].parse_result is None


class TestRehydrateIdentity:
    @pytest.mark.parametrize("checksum", ["x", "", "a" * 63])
    def test_unverifiable_checksum_requires_reimport_before_parsing(self, tmp_path, checksum, monkeypatch):
        log = tmp_path / "unchanged_time.log"
        log.write_text(_TIME_LOG, encoding="utf-8")
        source = _file(log)
        source.checksum = checksum
        monkeypatch.setattr("profiles.m350.profile.build_registry", lambda: pytest.fail("unverified source parsed"))
        with pytest.raises(ValueError, match="нужен повторный импорт"):
            sources_mod.rehydrate_parse_results([source])
        assert source.parse_result is None

    @pytest.mark.parametrize("changed", [_TIME_LOG.replace("30000", "40000"), _TIME_LOG + "another line\n"])
    def test_changed_content_or_size_requires_reimport(self, tmp_path, changed):
        log = tmp_path / "changed_time.log"
        log.write_text(_TIME_LOG, encoding="utf-8")
        source = _file(log)
        log.write_text(changed, encoding="utf-8")
        with pytest.raises(ValueError, match="нужен повторный импорт"):
            sources_mod.rehydrate_parse_results([source])
        assert source.parse_result is None

    def test_changed_mutable_nas_mirror_requires_reimport(self, tmp_path, store):
        log = tmp_path / "mirrored_time.log"
        log.write_text(_TIME_LOG, encoding="utf-8")
        source = _file(log)
        sources_mod.mirror_logs_to_object_store("s1", [source])
        log.unlink()
        store.objects[("raw-logs", "s1/mirrored_time.log")] = _TIME_LOG.replace("30000", "40000").encode()
        with pytest.raises(ValueError, match="нужен повторный импорт"):
            sources_mod.rehydrate_parse_results([source], "s1")
        assert source.parse_result is None

    def test_corrupt_immutable_nas_mirror_is_not_treated_as_missing(self, tmp_path, store):
        log = tmp_path / "immutable_time.log"
        log.write_text(_TIME_LOG, encoding="utf-8")
        source = _file(log)
        sources_mod.mirror_logs_to_object_store("s1", [source], immutable=True)
        name = source.metadata["shared_log_uri"].removeprefix("s3://raw-logs/")
        store.objects[("raw-logs", name)] = b"corrupt bytes"
        log.unlink()
        with pytest.raises(ValueError, match="нужен повторный импорт"):
            sources_mod.rehydrate_parse_results([source], "s1")
        assert source.parse_result is None

    @pytest.mark.parametrize("replace_inode", [False, True])
    def test_growth_or_rotation_during_parse_never_assigns_result(self, tmp_path, monkeypatch, replace_inode):
        import os
        from profiles.m350.profile import build_registry

        log = tmp_path / "rotating_time.log"
        log.write_text(_TIME_LOG, encoding="utf-8")
        source = _file(log)
        registry = build_registry()

        class ChangingRegistry:
            def parse(self, path, family, context):
                result = registry.parse(path, family, context)
                if replace_inode:
                    stat = path.stat()
                    replacement = path.with_suffix(".replacement")
                    replacement.write_bytes(path.read_bytes())
                    os.utime(replacement, ns=(stat.st_atime_ns, stat.st_mtime_ns))
                    replacement.replace(path)
                else:
                    path.write_text(_TIME_LOG + "another line\n", encoding="utf-8")
                return result

        monkeypatch.setattr("profiles.m350.profile.build_registry", ChangingRegistry)
        with pytest.raises(ValueError, match="нужен повторный импорт"):
            sources_mod.rehydrate_parse_results([source])
        assert source.parse_result is None

    def test_change_during_checksum_verification_never_parses(self, tmp_path, monkeypatch):
        log = tmp_path / "hashing_time.log"
        log.write_text(_TIME_LOG, encoding="utf-8")
        source = _file(log)
        original = sources_mod.sha256_file

        def hash_and_change(path):
            checksum = original(path)
            path.write_text(_TIME_LOG + "another line\n", encoding="utf-8")
            return checksum

        monkeypatch.setattr(sources_mod, "sha256_file", hash_and_change)
        with pytest.raises(ValueError, match="нужен повторный импорт"):
            sources_mod.rehydrate_parse_results([source])
        assert source.parse_result is None

    def test_filled_parse_result_is_not_reopened(self, tmp_path, monkeypatch):
        log = tmp_path / "parsed_time.log"
        log.write_text(_TIME_LOG, encoding="utf-8")
        source = _file(log)
        sources_mod.rehydrate_parse_results([source])
        result = source.parse_result
        log.write_text("a newer file", encoding="utf-8")
        monkeypatch.setattr(sources_mod, "sha256_file", lambda *args: pytest.fail("parsed source reopened"))
        assert sources_mod.rehydrate_parse_results([source])[0].parse_result is result

    def test_parser_failure_still_leaves_legacy_source_slim(self, tmp_path, monkeypatch):
        log = tmp_path / "unreadable_format_time.log"
        log.write_text(_TIME_LOG, encoding="utf-8")
        source = _file(log)

        class FailingRegistry:
            def parse(self, *args):
                raise RuntimeError("unrecognized legacy format")

        monkeypatch.setattr("profiles.m350.profile.build_registry", FailingRegistry)
        assert sources_mod.rehydrate_parse_results([source])[0].parse_result is None


def _stored_source(file, *, owner=None, start_ts=None):
    from core.config.settings import get_settings
    from domain.models.sessions import BuildSession
    from storage.db.session import session_scope

    with session_scope() as db:
        db.add(BuildSession(
            session_id="sources-1", origin_compute_node_id=owner or get_settings().compute_node_id,
            context={"runtime_payload": {"files": [file.model_dump(mode="json")],
                                         "group": {"start_ts": start_ts}}},
        ))


def test_changed_source_prevents_partial_maintenance_publication(tmp_path):
    from copy import deepcopy
    from domain.models.sessions import BuildSession
    from scripts.maintenance.backfill_session_overview import backfill
    from storage.db.session import SessionLocal

    good = tmp_path / "first_time.log"
    changed = tmp_path / "second_time.log"
    good.write_text(_TIME_LOG, encoding="utf-8")
    changed.write_text(_TIME_LOG, encoding="utf-8")
    _stored_source(_file(good))
    with SessionLocal() as db:
        row = db.get(BuildSession, "sources-1")
        context = deepcopy(row.context)
        context["runtime_payload"]["files"].append(_file(changed).model_dump(mode="json"))
        row.context = context
        db.commit()
    changed.write_text(_TIME_LOG.replace("30000", "40000"), encoding="utf-8")
    backfill(dry_run=False, force=True)
    with SessionLocal() as db:
        assert db.get(BuildSession, "sources-1").context == context


def test_detached_source_read_closes_sql_before_real_parse(tmp_path, store):
    from sqlalchemy import event
    from storage.db.session import SessionLocal, engine

    log = tmp_path / "23.03.2026_time.log"
    log.write_text(_TIME_LOG, encoding="utf-8")
    _stored_source(_file(log))
    held = set()
    def checkout(connection, record, proxy):
        held.add(id(connection))
    def checkin(connection, record):
        held.discard(id(connection))
    event.listen(engine, "checkout", checkout)
    event.listen(engine, "checkin", checkin)
    try:
        with SessionLocal() as db:
            sources = sources_mod.read_session_sources(db, "sources-1")
            assert not held and not db.in_transaction()
            files = sources_mod.rehydrate_session_sources(sources)
            assert len(files[0].parse_result.events) == 5
            assert not held and not db.in_transaction()
    finally:
        event.remove(engine, "checkout", checkout)
        event.remove(engine, "checkin", checkin)


@pytest.mark.parametrize("owner", ["foreign", "legacy-unassigned"])
def test_foreign_source_reconstruction_never_opens_files(monkeypatch, owner):
    from domain.services.compute_affinity import ComputeAffinityError

    sources = sources_mod.SessionSources("session-foreign", owner, [])
    monkeypatch.setattr(sources_mod, "rehydrate_parse_results", lambda *a: pytest.fail("foreign raw IO"))
    with pytest.raises(ComputeAffinityError):
        sources_mod.rehydrate_session_sources(sources, compute_node_id="this-pc")


def test_experimental_metrics_keep_all_results_without_holding_sql(tmp_path, monkeypatch):
    from api.routes import test_metrics
    from storage.db.session import SessionLocal

    log = tmp_path / "metrics_sensors.log"
    log.write_text("first measurement", encoding="utf-8")
    _stored_source(_file(log, SourceFileFamily.sensors_log))
    test_metrics._cache.clear()
    calls = []
    with SessionLocal() as db:
        def compute(path):
            assert not db.in_transaction()
            calls.append(path.read_text())
            return {"ruptures": [1], "pyod": [2], "other": "preserved"}
        monkeypatch.setattr(test_metrics, "compute_test_metrics", compute)
        first = test_metrics._get_test_metrics("sources-1", False, db)
        assert first == {"session_id": "sources-1", "ruptures": [1], "pyod": [2], "other": "preserved"}
        assert test_metrics._get_test_metrics("sources-1", False, db) == first
        assert len(calls) == 1
        log.write_text("changed and longer measurement", encoding="utf-8")
        assert test_metrics._get_test_metrics("sources-1", False, db) == first
        assert len(calls) == 2
        assert test_metrics._get_test_metrics("sources-1", True, db) == first
        assert len(calls) == 3
        assert not db.in_transaction()
    test_metrics._cache.clear()


def test_experimental_metrics_refuse_foreign_before_path_access(tmp_path, monkeypatch):
    from fastapi import HTTPException
    from api.routes import test_metrics
    from storage.db.session import SessionLocal

    _stored_source(_file(tmp_path / "foreign_sensors.log", SourceFileFamily.sensors_log), owner="foreign")
    monkeypatch.setattr(test_metrics, "_find_sensors_log_path", lambda *a: pytest.fail("foreign path access"))
    with SessionLocal() as db:
        with pytest.raises(HTTPException) as caught:
            test_metrics._get_test_metrics("sources-1", False, db)
        assert caught.value.status_code == 403
        assert not db.in_transaction()


def test_latest_experimental_session_uses_sql_order_without_full_payloads(tmp_path, monkeypatch):
    from api.routes import test_metrics
    from core.config.settings import get_settings
    from domain.models.sessions import BuildSession
    from storage.db.session import SessionLocal, session_scope
    from storage.repositories.session_reads import SessionReadsRepository

    _stored_source(_file(tmp_path / "sensors.log"), start_ts="2026-03-23T10:00:00+00:00")
    with session_scope() as db:
        db.add_all([
            BuildSession(session_id="earlier", origin_compute_node_id=get_settings().compute_node_id,
                         context={"runtime_payload": {
                "files": [], "group": {"start_ts": "2026-03-22T10:00:00+00:00"}}}),
            BuildSession(session_id="foreign-later", origin_compute_node_id="foreign", context={"runtime_payload": {
                "files": [], "group": {"start_ts": "2026-03-24T10:00:00+00:00"}}}),
            BuildSession(session_id="empty", context={}),
        ])
    monkeypatch.setattr(SessionReadsRepository, "list_groups", lambda *a, **k: pytest.fail("full history load"))
    with SessionLocal() as db:
        assert test_metrics._latest_session_id(db) == "sources-1"
        assert not db.in_transaction()


@pytest.mark.parametrize("session_id,file_name", [("..", "t.log"), ("s1", ".."), ("", "t.log")])
def test_mirror_cache_rejects_non_components(store, session_id, file_name):
    assert sources_mod.fetch_shared_log(session_id, file_name) is None
