"""Actual intake/receipt/claim/publication flow, with no raw parsing or NAS IO."""

from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from types import SimpleNamespace

import pytest
from psycopg.errors import ConnectionFailure
from sqlalchemy.exc import OperationalError

from analytics.prediction.timing_snapshot import prepare_layer_timings
from core.config.settings import get_settings
from domain.enums.common import ImportJobStatus
from domain.models.prints import PrintRecord
from domain.models.quality import QualityOutcome
from domain.models.sessions import BuildSession, ImportJob, ReportArtifact
from domain.services.import_jobs import detect_import_candidate
from domain.services.importing import uploads
from domain.services.importing.contracts import ImportExecutionResult
from domain.services.importing.fence import ImportFence, StaleImportLeaseError
from domain.services.importing.publication import prepare_import_reports, publish_import, session_publication_token
from domain.services.importing.recovery import recover_upload_receipt
from domain.services.importing.requests import apply_import_action
from domain.services.print_cards.attachments import upload_attachment
from domain.services.print_cards.cards import update_card
from domain.services.print_cards.contracts import CardError
from domain.services.print_linking import auto_link_print_records
from storage.db.session import session_scope
from storage.repositories.import_jobs import ImportJobsRepository
from storage.repositories.prints_repo import PrintsRepository, captured_card_link_intent
from storage.repositories.runtime import RuntimeRepository


DATE = datetime(2026, 7, 17, tzinfo=timezone.utc)


@pytest.fixture
def intake(tmp_path, monkeypatch):
    settings = get_settings()
    root = tmp_path / 'own-raw'
    root.mkdir()
    monkeypatch.setattr(settings, 'raw_logs_container_path', str(root))
    monkeypatch.setattr(settings, 'require_operator_import_confirmation', True)
    with session_scope() as db:
        db.add_all([BuildSession(session_id=sid, origin_compute_node_id=settings.compute_node_id,
                                 start_ts=DATE + timedelta(days=index))
                    for index, sid in enumerate(('previous', 'imported', 'operator'))])
        db.flush()
        db.add(PrintRecord(
            record_id='intent-card', name='Card', session_id='previous',
            origin_compute_node_id=settings.compute_node_id,
            metadata_json={'retained': 'yes', 'session_link_confirmed': True,
                           'session_link_evidence': {'method': 'operator_selected_session',
                                                     'session_id': 'previous', 'confirmed_by': 'initial'}},
        ))
        db.flush()
        RuntimeRepository(db).create_quality_outcome({
            'outcome_id': 'intent-quality', 'print_record_id': 'intent-card', 'session_id': 'previous',
            'is_final': True, 'result': 'good', 'timestamp': DATE.isoformat(), 'created_by': 'initial',
        })
    return settings, root


def _edit(settings, session_id):
    with session_scope() as db:
        repo = PrintsRepository(db)
        record = repo.get_print_record('intent-card')
        return update_card(repo, 'intent-card', {
            'session_id': session_id, 'expected_revision': record['revision'],
        }, compute_node_id=settings.compute_node_id, actor='later-operator')


def _upload(settings, callback=None):
    class Stream(io.BytesIO):
        def read(self, size=-1):
            nonlocal callback
            if callback:
                action, callback = callback, None
                action()
            return super().read(size)
    with session_scope() as db:
        return uploads.upload_log_batch(PrintsRepository(db), [
            uploads.LogUpload('17.07.2026_time.log', Stream(b'own synthetic input')),
        ], settings=settings, record_id='intent-card')['jobs'][0]['import_job_id']


def _prepare(job_id, settings):
    with session_scope() as db:
        current = ImportJobsRepository(db).get_import_job(job_id)
        if current.status == ImportJobStatus.awaiting_operator_confirmation:
            apply_import_action(db, job_id, 'confirm', settings=settings, actor='intake-operator')
    with session_scope() as db:
        job = ImportJobsRepository(db).claim_next_import_job(
            owner_node_id=settings.compute_node_id, lease_owner='intent-worker',
        )
        assert job.import_job_id == job_id
        token = session_publication_token(db.get(BuildSession, 'imported'))
    fence = ImportFence(job_id, settings.compute_node_id, job.lease_owner, job.lease_generation)
    prepared = prepare_layer_timings([], owner_node_id=settings.compute_node_id, source=fence.as_dict())
    job.status, job.session_ids, job.report_ids = ImportJobStatus.done, ['imported'], ['intent-report']
    result = ImportExecutionResult(
        job=job, previous_session_tokens={'imported': token}, layer_timings={'imported': prepared},
        sessions={'imported': {'files': [], 'group': {
            'classification': 'REAL_PRINT', 'start_ts': (DATE + timedelta(days=1)).isoformat(),
            'timing_publication_id': prepared.manifest['publication_id'],
        }}}, reports={'intent-report': {
            'report_id': 'intent-report', 'session_id': 'imported',
            'timing_publication': prepared.manifest, 'timeline': [],
        }},
    )
    return result, fence, prepare_import_reports(result)


def _publish(prepared):
    result, fence, reports = prepared
    with session_scope() as db:
        publish_import(db, result, fence=fence, prepared_reports=reports)


def _assert_completed(job_id, settings, expected_session):
    with session_scope() as db:
        repo = PrintsRepository(db)
        record = repo.get_print_record('intent-card')
        assert record['session_id'] == expected_session
        assert db.get(QualityOutcome, 'intent-quality').session_id == expected_session
        assert record['origin_compute_node_id'] == settings.compute_node_id
        job = db.get(ImportJob, job_id)
        assert job.status == 'done' and job.owner_node_id == settings.compute_node_id
        assert job.lease_owner is None and job.lease_generation >= 1
        assert db.get(ReportArtifact, 'intent-report') is not None
        return record, job.audit_trail


def test_upload_intentionally_replaces_unchanged_existing_link(intake):
    settings, _ = intake
    job_id = _upload(settings)
    prepared = _prepare(job_id, settings)
    intent = captured_card_link_intent(prepared[0].job.audit_trail, 'intent-card')
    assert intent['expected_session_id'] == 'previous'
    _publish(prepared)
    record, audit = _assert_completed(job_id, settings, 'imported')
    assert record['metadata_json']['session_link_evidence']['method'] == 'operator_card_upload'
    assert captured_card_link_intent(audit, 'intent-card') == intent


@pytest.mark.parametrize('phase,choice', [
    ('copy', 'operator'), ('publication', 'operator'), ('publication', None), ('publication', 'previous'),
])
def test_later_operator_link_unlink_or_evidence_wins_without_losing_analysis(intake, phase, choice):
    settings, _ = intake
    job_id = _upload(settings, (lambda: _edit(settings, choice)) if phase == 'copy' else None)
    prepared = _prepare(job_id, settings)
    if phase == 'publication':
        _edit(settings, choice)
    with session_scope() as db:
        before = PrintsRepository(db).get_print_record('intent-card')
    _publish(prepared)
    record, audit = _assert_completed(job_id, settings, choice)
    assert record['printed_at'] == before['printed_at']
    assert record['metadata_json'] == before['metadata_json']
    assert record['revision'] == before['revision']
    skipped = [item for item in audit if item['action'] == 'card_link_skipped']
    assert skipped[-1]['details']['current_session_id'] == choice
    assert skipped[-1]['details']['requested_session_id'] == 'imported'


def test_notes_and_actual_photo_upload_do_not_cancel_log_replacement(intake):
    settings, _ = intake
    job_id = _upload(settings)
    prepared = _prepare(job_id, settings)
    with session_scope() as db:
        repo = PrintsRepository(db)
        current = repo.get_print_record('intent-card')
        update_card(repo, 'intent-card', {'notes': 'retain operator note', 'expected_revision': current['revision']},
                    compute_node_id=settings.compute_node_id, actor='other-operator')
    class FakeStore:
        def is_available(self):
            return True
        def put_file_verified(self, bucket, name, path, *, expected_sha256, expected_size, content_type):
            content = path.read_bytes()
            assert len(content) == expected_size and hashlib.sha256(content).hexdigest() == expected_sha256
            return f's3://{bucket}/{name}'
    with session_scope() as db:
        uploaded = upload_attachment(PrintsRepository(db), 'intent-card', io.BytesIO(b'photo'),
                                     file_name='photo.jpg', file_type='photo', settings=settings, store_factory=FakeStore)
        assert not uploaded.queued
    _publish(prepared)
    record, _ = _assert_completed(job_id, settings, 'imported')
    assert record['notes'] == 'retain operator note'
    with session_scope() as db:
        assert len(PrintsRepository(db).list_print_files('intent-card')) == 1


def test_receipt_recovery_keeps_original_intent_after_operator_changes(intake, monkeypatch):
    settings, root = intake
    def unavailable(*args, **kwargs):
        raise OperationalError(None, None, ConnectionFailure('connection lost'))
    with monkeypatch.context() as patch:
        patch.setattr(uploads, 'publish_prepared_browser_upload', unavailable)
        with pytest.raises(CardError) as raised:
            _upload(settings)
        assert raised.value.code == 'storage_unavailable'
    receipt_path = next((root / 'incoming').glob('*.json'))
    receipt = json.loads(receipt_path.read_text())
    capture = captured_card_link_intent(receipt['job']['audit_trail'], 'intent-card')
    _edit(settings, 'operator')
    assert recover_upload_receipt(receipt_path, settings=settings)['state'] == 'registered'
    job_id = receipt['import_job_id']
    prepared = _prepare(job_id, settings)
    assert captured_card_link_intent(prepared[0].job.audit_trail, 'intent-card') == capture
    _publish(prepared)
    _, audit = _assert_completed(job_id, settings, 'operator')
    assert captured_card_link_intent(audit, 'intent-card') == capture


def test_explicit_retry_does_not_recapture_operator_choice(intake):
    settings, _ = intake
    job_id = _upload(settings)
    prepared = _prepare(job_id, settings)
    capture = captured_card_link_intent(prepared[0].job.audit_trail, 'intent-card')
    _publish(prepared)
    _edit(settings, 'operator')
    with session_scope() as db:
        apply_import_action(db, job_id, 'retry', settings=settings, actor='retry-operator')
    retried = _prepare(job_id, settings)
    assert retried[1].lease_generation == 2
    assert captured_card_link_intent(retried[0].job.audit_trail, 'intent-card') == capture
    _publish(retried)
    _assert_completed(job_id, settings, 'operator')


def test_manual_unlink_is_not_undone_by_later_hint_sweep(intake):
    settings, _ = intake
    job_id = _upload(settings)
    _publish(_prepare(job_id, settings))
    _edit(settings, None)
    with session_scope() as db:
        assert auto_link_print_records(db, origin_compute_node_id=settings.compute_node_id) == []
    _assert_completed(job_id, settings, None)


@pytest.mark.parametrize('current', [None, 'imported', 'previous'])
def test_legacy_jobs_only_fill_empty_or_keep_same_link(intake, current):
    settings, root = intake
    with session_scope() as db:
        repo = PrintsRepository(db)
        repo.update_print_record('intent-card', {
            'session_id': current, 'metadata_json': {'retained': 'legacy', 'session_link_confirmed': True},
        })
        legacy = detect_import_candidate(root, settings=settings, print_record_id='intent-card', file_snapshot={})
        ImportJobsRepository(db).save_import_job(legacy.job)
    _publish(_prepare(legacy.job.import_job_id, settings))
    record, _ = _assert_completed(legacy.job.import_job_id, settings, current or 'imported')
    if current:
        assert record['metadata_json'] == {'retained': 'legacy', 'session_link_confirmed': True}


def test_link_intent_cannot_bypass_owner_or_generation_fence(intake):
    settings, _ = intake
    job_id = _upload(settings)
    prepared = _prepare(job_id, settings)
    result, fence, reports = prepared
    for wrong in (ImportFence(job_id, 'foreign-node', fence.lease_owner, fence.lease_generation),
                  ImportFence(job_id, settings.compute_node_id, fence.lease_owner, fence.lease_generation + 1)):
        with pytest.raises(StaleImportLeaseError), session_scope() as db:
            publish_import(db, result, fence=wrong, prepared_reports=reports)
        with session_scope() as db:
            assert PrintsRepository(db).get_print_record('intent-card')['session_id'] == 'previous'
            assert db.get(ReportArtifact, 'intent-report') is None
    _publish(prepared)
    _assert_completed(job_id, settings, 'imported')


def test_generic_pre_read_identity_cannot_overwrite_new_operator_link(intake):
    settings, _ = intake
    with session_scope() as db:
        PrintsRepository(db).update_print_record('intent-card', {'session_id': None, 'metadata_json': {}})
    with session_scope() as db:
        cached = db.get(PrintRecord, 'intent-card')
        record = PrintsRepository(db).get_print_record('intent-card')
        from storage.repositories.prints_repo import card_link_fingerprint
        token = card_link_fingerprint(record['session_id'], record['metadata_json'])
        _edit(settings, 'operator')
        assert not PrintsRepository(db).link_session(
            'intent-card', 'imported', compute_node_id=settings.compute_node_id,
            expected_link_fingerprint=token,
        )
        assert cached.session_id == 'operator'
    with session_scope() as db:
        assert PrintsRepository(db).get_print_record('intent-card')['session_id'] == 'operator'


def _completed_reuploads_waiting_for_same_session(settings, count=1):
    """A temporary conflicting card prevents attachment, not analysis publication."""
    old = _upload(settings)
    _publish(_prepare(old, settings))
    _edit(settings, None)
    with session_scope() as db:
        db.add(PrintRecord(record_id='temporary-holder', name='Temporary card', session_id='imported',
                           origin_compute_node_id=settings.compute_node_id))
    matching = []
    for _ in range(count):
        job_id = _upload(settings)
        _publish(_prepare(job_id, settings))
        matching.append(job_id)
    with session_scope() as db:
        repo = PrintsRepository(db)
        holder = repo.get_print_record('temporary-holder')
        update_card(repo, 'temporary-holder', {'session_id': None, 'expected_revision': holder['revision']},
                    compute_node_id=settings.compute_node_id, actor='holder-correction')
    return old, matching


def _ordered_job_query(db, monkeypatch, order):
    scalars = db.scalars
    def ordered(statement, *args, **kwargs):
        result = scalars(statement, *args, **kwargs)
        if statement.column_descriptions[0]['entity'] is ImportJob:
            rows = sorted(result.all(), key=lambda job: order.index(job.import_job_id))
            return SimpleNamespace(all=lambda: rows)
        return result
    monkeypatch.setattr(db, 'scalars', ordered)


@pytest.mark.parametrize('matching_first', [True, False])
def test_reupload_hint_uses_matching_intent_independent_of_query_order(intake, monkeypatch, matching_first):
    settings, _ = intake
    old, matching = _completed_reuploads_waiting_for_same_session(settings)
    new = matching[0]
    with session_scope() as db:
        _ordered_job_query(db, monkeypatch, [new, old] if matching_first else [old, new])
        assert auto_link_print_records(db, origin_compute_node_id=settings.compute_node_id) == [
            {'record_id': 'intent-card', 'session_id': 'imported'},
        ]
    record, _ = _assert_completed(new, settings, 'imported')
    assert record['metadata_json']['session_link_evidence']['import_job_id'] == new


@pytest.mark.parametrize('reverse', [True, False])
def test_equivalent_matching_hint_intents_have_stable_tie_break(intake, monkeypatch, reverse):
    settings, _ = intake
    old, matching = _completed_reuploads_waiting_for_same_session(settings, count=2)
    with session_scope() as db:
        _ordered_job_query(db, monkeypatch, [old, *sorted(matching, reverse=reverse)])
        auto_link_print_records(db, origin_compute_node_id=settings.compute_node_id)
    record, _ = _assert_completed(matching[0], settings, 'imported')
    assert record['metadata_json']['session_link_evidence']['import_job_id'] == min(matching)


def test_unavailable_second_sid_still_makes_completed_hint_ambiguous(intake):
    settings, _ = intake
    old, matching = _completed_reuploads_waiting_for_same_session(settings)
    with session_scope() as db:
        db.add(PrintRecord(record_id='claimed-other', name='Other print', session_id='operator',
                           origin_compute_node_id=settings.compute_node_id))
        # A completed multi-session result remains ambiguous even when one
        # session is already claimed and its intent no longer matches.
        db.get(ImportJob, old).session_ids = ['imported', 'operator']
    with session_scope() as db:
        assert auto_link_print_records(db, origin_compute_node_id=settings.compute_node_id) == []
    _assert_completed(matching[0], settings, None)


def test_selected_hint_gets_one_cas_without_recapture_after_operator_change(intake, monkeypatch):
    settings, _ = intake
    old, matching = _completed_reuploads_waiting_for_same_session(settings, count=2)
    link = PrintsRepository.link_session
    attempts = []
    def concurrent_change(repo, record_id, session_id, session_start=None, **kwargs):
        attempts.append(kwargs['expected_link_fingerprint'])
        _edit(settings, 'operator')
        return link(repo, record_id, session_id, session_start, **kwargs)
    monkeypatch.setattr(PrintsRepository, 'link_session', concurrent_change)
    with session_scope() as db:
        _ordered_job_query(db, monkeypatch, [old, *matching])
        assert auto_link_print_records(db, origin_compute_node_id=settings.compute_node_id) == []
    assert len(attempts) == 1
    _assert_completed(matching[0], settings, 'operator')
