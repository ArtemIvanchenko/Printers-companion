from domain.models.jobs import BackgroundJob
from storage.db.session import session_scope
from storage.repositories.jobs_repo import JobsRepository
from storage.repositories.prints_repo import PrintsRepository


def test_stale_queued_estimate_is_rejected_before_geometry(monkeypatch):
    from worker.estimate_tasks import process_next_estimate
    with session_scope() as db:
        record = PrintsRepository(db).create_print_record({'name': 'changed', 'material': 'steel',
                                                          'origin_compute_node_id': 'operator-test'})
        job = JobsRepository(db).enqueue(job_type='print_estimate', owner_node_id='operator-test',
            entity_type='print_record', entity_id=record['record_id'], idempotency_key='stale-test',
            payload={'record_id': record['record_id'], 'record_revision': record['revision'] - 1,
                     'owner_node_id': 'operator-test'})
    def forbidden(*args, **kwargs):
        raise AssertionError('Stale jobs must not prepare or slice geometry')
    monkeypatch.setattr('api.routes.prints._prepare_prediction_inputs', forbidden)
    assert process_next_estimate('worker-test', 'operator-test')
    with session_scope() as db:
        row = db.get(BackgroundJob, job['job_id'])
        assert row.status == 'failed'
        assert 'Карточка изменилась' in row.error
