from datetime import datetime, timezone

from core.config.settings import Settings
from domain.enums.common import ImportJobStatus
from domain.services.import_jobs import ImportJobRecord, ImportPersistenceError
from worker.tasks import _apply_import_failure_policy


def _job() -> ImportJobRecord:
    return ImportJobRecord(
        import_job_id="import_retry",
        owner_node_id="operator-01",
        source_path="/local/logs",
        source_name="logs",
        lease_owner="operator-01:worker",
        lease_generation=8,
        stability_check_attempts=9,
    )


def test_nas_persistence_failure_does_not_spend_import_retry_budget():
    job = _job()
    now = datetime.now(timezone.utc)
    settings = Settings(
        nas_sync_retry_min_seconds=5,
        nas_sync_retry_max_seconds=300,
        file_stability_max_retries=10,
    )

    _apply_import_failure_policy(
        job,
        ImportPersistenceError("NAS database unavailable"),
        settings=settings,
        now=now,
    )

    assert job.status == ImportJobStatus.postponed
    assert job.stability_check_attempts == 9
    assert job.postponed_until is not None
    assert (job.postponed_until - now).total_seconds() == 300
    assert job.lease_owner is None


def test_unexpected_import_failure_remains_bounded():
    job = _job()
    now = datetime.now(timezone.utc)
    settings = Settings(file_stability_max_retries=10)

    _apply_import_failure_policy(
        job,
        RuntimeError("invalid parser state"),
        settings=settings,
        now=now,
    )

    assert job.status == ImportJobStatus.failed
    assert job.stability_check_attempts == 10
    assert job.postponed_until is None
