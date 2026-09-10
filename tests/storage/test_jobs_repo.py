from datetime import datetime, timedelta, timezone

from domain.models.jobs import BackgroundJob
from storage.db.session import SessionLocal
from storage.repositories.jobs_repo import JobsRepository


def test_enqueue_is_idempotent_and_job_can_complete():
    with SessionLocal() as db:
        repo = JobsRepository(db)
        first = repo.enqueue(
            job_type="print_estimate",
            owner_node_id="operator-01",
            entity_type="print_record",
            entity_id="pr_1",
            idempotency_key="print_estimate:pr_1:1",
            payload={"record_id": "pr_1"},
        )
        second = repo.enqueue(
            job_type="print_estimate",
            owner_node_id="operator-01",
            entity_type="print_record",
            entity_id="pr_1",
            idempotency_key="print_estimate:pr_1:1",
            payload={"record_id": "pr_1"},
        )
        assert second["job_id"] == first["job_id"]

        claimed = repo.claim_next(
            "print_estimate", owner_node_id="operator-01", lease_owner="worker-1"
        )
        assert claimed["job_id"] == first["job_id"]
        assert claimed["status"] == "running"
        assert claimed["attempts"] == 1

        done = repo.complete(
            first["job_id"],
            {"hours": 3.5},
            lease_owner="worker-1",
            lease_generation=claimed["lease_generation"],
        )
        assert done["status"] == "done"
        assert done["result"] == {"hours": 3.5}
        assert repo.claim_next(
            "print_estimate", owner_node_id="operator-01", lease_owner="worker-1"
        ) is None


def test_expired_lease_is_reclaimed_after_worker_crash():
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        repo = JobsRepository(db)
        queued = repo.enqueue(
            job_type="print_estimate",
            owner_node_id="operator-01",
            entity_type="print_record",
            entity_id="pr_2",
            idempotency_key="print_estimate:pr_2:1",
            payload={"record_id": "pr_2"},
        )
        row = db.get(BackgroundJob, queued["job_id"])
        row.status = "running"
        row.attempts = 1
        row.lease_owner = "dead-worker"
        row.lease_until = now - timedelta(seconds=1)
        db.flush()

        reclaimed = repo.claim_next(
            "print_estimate",
            owner_node_id="operator-01",
            lease_owner="worker-2",
            now=now,
        )
        assert reclaimed["job_id"] == queued["job_id"]
        assert reclaimed["lease_owner"] == "worker-2"
        assert reclaimed["attempts"] == 2


def test_crash_on_final_attempt_is_closed_as_failed():
    now = datetime.now(timezone.utc) + timedelta(seconds=1)
    with SessionLocal() as db:
        repo = JobsRepository(db)
        queued = repo.enqueue(
            job_type="print_estimate",
            owner_node_id="operator-01",
            entity_type="print_record",
            entity_id="pr_final_crash",
            idempotency_key="print_estimate:pr_final_crash:1",
            payload={"record_id": "pr_final_crash"},
            max_attempts=1,
        )
        claimed = repo.claim_next(
            "print_estimate",
            owner_node_id="operator-01",
            lease_owner="dead-worker",
            now=now,
            lease_seconds=60,
        )
        assert claimed is not None and claimed["attempts"] == 1
        row = db.get(BackgroundJob, queued["job_id"])
        row.lease_until = now - timedelta(seconds=1)
        db.flush()

        assert repo.claim_next(
            "print_estimate",
            owner_node_id="operator-01",
            lease_owner="replacement-worker",
            now=now,
        ) is None
        assert row.status == "failed"
        assert row.finished_at == now
        assert row.lease_owner is None


def test_worker_cannot_claim_another_operator_pcs_job():
    with SessionLocal() as db:
        repo = JobsRepository(db)
        queued = repo.enqueue(
            job_type="print_estimate",
            owner_node_id="operator-01",
            entity_type="print_record",
            entity_id="pr_affinity",
            idempotency_key="print_estimate:operator-01:pr_affinity:1",
            payload={"record_id": "pr_affinity"},
        )

        assert repo.claim_next(
            "print_estimate",
            owner_node_id="operator-02",
            lease_owner="operator-02:worker",
        ) is None
        claimed = repo.claim_next(
            "print_estimate",
            owner_node_id="operator-01",
            lease_owner="operator-01:worker",
        )
        assert claimed is not None
        assert claimed["job_id"] == queued["job_id"]
        assert claimed["owner_node_id"] == "operator-01"


def test_expired_lease_cannot_finalize_after_a_new_generation_claims_job():
    with SessionLocal() as db:
        repo = JobsRepository(db)
        queued = repo.enqueue(
            job_type="print_estimate",
            owner_node_id="operator-01",
            entity_type="print_record",
            entity_id="pr_fence",
            idempotency_key="print_estimate:operator-01:pr_fence:1",
            payload={"record_id": "pr_fence"},
        )
        now = datetime.now(timezone.utc) + timedelta(seconds=1)
        old = repo.claim_next(
            "print_estimate",
            owner_node_id="operator-01",
            lease_owner="old-worker",
            now=now,
            lease_seconds=60,
        )
        row = db.get(BackgroundJob, queued["job_id"])
        row.lease_until = now - timedelta(seconds=1)
        db.flush()
        new = repo.claim_next(
            "print_estimate",
            owner_node_id="operator-01",
            lease_owner="new-worker",
            now=now,
        )

        assert new["lease_generation"] == old["lease_generation"] + 1
        assert repo.complete(
            queued["job_id"],
            {"stale": True},
            lease_owner="old-worker",
            lease_generation=old["lease_generation"],
        ) is None
        completed = repo.complete(
            queued["job_id"],
            {"stale": False},
            lease_owner="new-worker",
            lease_generation=new["lease_generation"],
        )
        assert completed is not None
        assert completed["result"] == {"stale": False}


def test_only_current_unexpired_generation_can_renew_lease():
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        repo = JobsRepository(db)
        queued = repo.enqueue(
            job_type="print_estimate",
            owner_node_id="operator-01",
            entity_type="print_record",
            entity_id="pr_heartbeat",
            idempotency_key="print_estimate:operator-01:pr_heartbeat:1",
            payload={"record_id": "pr_heartbeat"},
        )
        claim_time = now + timedelta(seconds=1)
        claimed = repo.claim_next(
            "print_estimate",
            owner_node_id="operator-01",
            lease_owner="worker-current",
            now=claim_time,
            lease_seconds=60,
        )

        assert not repo.renew_lease(
            queued["job_id"],
            lease_owner="worker-stale",
            lease_generation=claimed["lease_generation"],
            now=claim_time + timedelta(seconds=10),
        )
        assert repo.renew_lease(
            queued["job_id"],
            lease_owner="worker-current",
            lease_generation=claimed["lease_generation"],
            lease_seconds=120,
            now=claim_time + timedelta(seconds=10),
        )
        row = db.get(BackgroundJob, queued["job_id"])
        lease_until = row.lease_until
        if lease_until.tzinfo is None:
            lease_until = lease_until.replace(tzinfo=timezone.utc)
        assert lease_until == claim_time + timedelta(seconds=130)

        row.lease_until = now
        db.flush()
        assert not repo.renew_lease(
            queued["job_id"],
            lease_owner="worker-current",
            lease_generation=claimed["lease_generation"],
            now=now + timedelta(seconds=1),
        )


def test_infrastructure_deferral_does_not_consume_algorithm_retry_budget():
    with SessionLocal() as db:
        repo = JobsRepository(db)
        queued = repo.enqueue(
            job_type="print_estimate",
            owner_node_id="operator-01",
            entity_type="print_record",
            entity_id="pr_nas_retry",
            idempotency_key="print_estimate:operator-01:pr_nas_retry:1",
            payload={"record_id": "pr_nas_retry"},
            max_attempts=1,
        )
        claimed = repo.claim_next(
            "print_estimate",
            owner_node_id="operator-01",
            lease_owner="worker-current",
        )
        assert claimed is not None and claimed["attempts"] == 1

        deferred = repo.defer_infrastructure(
            queued["job_id"],
            "NAS database unavailable",
            lease_owner="worker-current",
            lease_generation=claimed["lease_generation"],
        )

        assert deferred is not None
        assert deferred["status"] == "pending"
        assert deferred["attempts"] == 0
        assert deferred["lease_owner"] is None
        assert deferred["available_at"] is not None
