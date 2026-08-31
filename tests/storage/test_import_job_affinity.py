from datetime import datetime, timedelta, timezone

from core.config.settings import Settings
from api.routes.imports import create_detected_import
from domain.enums.common import ImportJobStatus
from domain.models.sessions import ImportJob
from domain.services.import_jobs import detect_import_candidate
from storage.db.session import SessionLocal
from storage.repositories.runtime import RuntimeRepository


def _queued(path, node_id: str):
    settings = Settings(
        app_env="test",
        compute_node_id=node_id,
        require_operator_import_confirmation=False,
    )
    job = detect_import_candidate(path, settings=settings).job
    job.status = ImportJobStatus.checking_stability
    job.confirmed_by = "operator"
    return job


def test_same_container_path_is_scoped_to_its_operator_pc(tmp_path):
    source = tmp_path / "same-mounted-path"
    source.mkdir()
    (source / "time.log").write_text("x", encoding="utf-8")

    with SessionLocal() as db:
        repo = RuntimeRepository(db)
        job_a = _queued(source, "operator-01")
        job_b = _queued(source, "operator-02")
        repo.save_import_job(job_a)
        repo.save_import_job(job_b)
        db.commit()

    with SessionLocal() as db:
        repo = RuntimeRepository(db)
        claimed_a = repo.claim_next_import_job(
            owner_node_id="operator-01",
            lease_owner="operator-01:worker",
        )
        db.commit()
        assert claimed_a is not None
        assert claimed_a.import_job_id == job_a.import_job_id
        assert claimed_a.owner_node_id == "operator-01"
        assert repo.claim_next_import_job(
            owner_node_id="operator-01",
            lease_owner="operator-01:other",
        ) is None

    with SessionLocal() as db:
        claimed_b = RuntimeRepository(db).claim_next_import_job(
            owner_node_id="operator-02",
            lease_owner="operator-02:worker",
        )
        assert claimed_b is not None
        assert claimed_b.import_job_id == job_b.import_job_id


def test_reclaimed_import_increments_fencing_generation(tmp_path):
    source = tmp_path / "batch"
    source.mkdir()
    (source / "time.log").write_text("x", encoding="utf-8")
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        repo = RuntimeRepository(db)
        job = _queued(source, "operator-01")
        repo.save_import_job(job)
        db.commit()

    with SessionLocal() as db:
        repo = RuntimeRepository(db)
        old = repo.claim_next_import_job(
            owner_node_id="operator-01",
            lease_owner="old-worker",
            now=now,
            lease_seconds=60,
        )
        row = repo.db.get(ImportJob, job.import_job_id)
        row.lease_until = now - timedelta(seconds=1)
        db.commit()

    with SessionLocal() as db:
        new = RuntimeRepository(db).claim_next_import_job(
            owner_node_id="operator-01",
            lease_owner="new-worker",
            now=now,
        )
        assert new is not None
        assert new.lease_generation == old.lease_generation + 1
        assert new.lease_owner == "new-worker"


def test_import_heartbeat_is_owner_and_generation_fenced(tmp_path):
    source = tmp_path / "heartbeat-batch"
    source.mkdir()
    (source / "time.log").write_text("x", encoding="utf-8")
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        repo = RuntimeRepository(db)
        job = _queued(source, "operator-01")
        repo.save_import_job(job)
        db.commit()

    with SessionLocal() as db:
        repo = RuntimeRepository(db)
        claimed = repo.claim_next_import_job(
            owner_node_id="operator-01",
            lease_owner="current-worker",
            now=now,
            lease_seconds=60,
        )
        assert claimed is not None
        assert not repo.renew_import_job_lease(
            job.import_job_id,
            lease_owner="stale-worker",
            lease_generation=claimed.lease_generation,
            now=now + timedelta(seconds=10),
        )
        assert repo.renew_import_job_lease(
            job.import_job_id,
            lease_owner="current-worker",
            lease_generation=claimed.lease_generation,
            lease_seconds=120,
            now=now + timedelta(seconds=10),
        )
        row = db.get(ImportJob, job.import_job_id)
        lease_until = row.lease_until
        if lease_until.tzinfo is None:
            lease_until = lease_until.replace(tzinfo=timezone.utc)
        assert lease_until == now + timedelta(seconds=130)


def test_late_card_link_preserves_an_already_claimed_worker_lease(tmp_path, monkeypatch):
    source = tmp_path / "watcher-upload-race"
    source.mkdir()
    (source / "time.log").write_text("x", encoding="utf-8")
    settings = Settings(
        app_env="test",
        compute_node_id="operator-race",
        require_operator_import_confirmation=False,
    )
    monkeypatch.setattr("api.routes.imports.get_settings", lambda: settings)

    with SessionLocal() as db:
        repo = RuntimeRepository(db)
        job = _queued(source, "operator-race")
        repo.save_import_job(job)
        db.commit()

    with SessionLocal() as db:
        repo = RuntimeRepository(db)
        claimed = repo.claim_next_import_job(
            owner_node_id="operator-race",
            lease_owner="operator-race:worker",
        )
        db.commit()
        assert claimed is not None

    with SessionLocal() as db:
        result = create_detected_import(
            str(source),
            RuntimeRepository(db),
            print_record_id="pr_exact",
        )
        db.commit()
        assert result.job.import_job_id == job.import_job_id
        assert result.job.print_record_id == "pr_exact"
        assert result.job.lease_owner == "operator-race:worker"
        assert result.job.lease_generation == claimed.lease_generation


def test_duplicate_manifest_is_hashed_before_main_nas_transaction(tmp_path, monkeypatch):
    old_source = tmp_path / "old" / "batch"
    new_source = tmp_path / "new" / "batch"
    old_source.mkdir(parents=True)
    new_source.mkdir(parents=True)
    (old_source / "time.log").write_text("same", encoding="utf-8")
    (new_source / "time.log").write_text("same", encoding="utf-8")
    settings = Settings(
        app_env="test",
        compute_node_id="operator-local-hash",
        require_operator_import_confirmation=False,
    )
    monkeypatch.setattr("api.routes.imports.get_settings", lambda: settings)

    old_job = _queued(old_source, settings.compute_node_id)
    old_job.status = ImportJobStatus.done
    old_job.checksum_manifest = {"time.log": "same-sha"}
    with SessionLocal() as seed_db:
        RuntimeRepository(seed_db).save_import_job(old_job)
        seed_db.commit()

    with SessionLocal() as db:
        def local_manifest(path):
            assert path == new_source
            assert not db.in_transaction()
            return {"time.log": "same-sha"}

        monkeypatch.setattr(
            "domain.services.import_jobs.calculate_checksum_manifest",
            local_manifest,
        )
        result = create_detected_import(str(new_source), RuntimeRepository(db))

        assert result.job.import_job_id == old_job.import_job_id
