from contextlib import contextmanager
from threading import Event

import pytest
from sqlalchemy.exc import OperationalError, TimeoutError as SQLAlchemyTimeoutError

from worker import estimate_tasks


class ControlledStop(Event):
    """Record retry delays without sleeping or contacting a real NAS."""

    def __init__(self, stop_after_waits=None):
        super().__init__()
        self.delays = []
        self.stop_after_waits = stop_after_waits

    def wait(self, timeout=None):
        self.delays.append(timeout)
        if self.stop_after_waits and len(self.delays) >= self.stop_after_waits:
            self.set()
        return self.is_set()


@pytest.mark.parametrize('failure', [
    OperationalError('claim', {}, ConnectionError('NAS disconnected')),
    SQLAlchemyTimeoutError('connection pool exhausted'),
])
def test_claim_outage_does_not_kill_estimator_and_recovery_uses_same_owner(monkeypatch, failure):
    stop = ControlledStop()
    calls = []

    def estimate(owner, node):
        calls.append((owner, node))
        if len(calls) == 1:
            raise failure
        stop.set()
        return True

    monkeypatch.setattr(estimate_tasks, 'process_next_estimate', estimate)
    monkeypatch.setattr('worker.model_tasks.process_next_model_task', lambda *args: False)
    estimate_tasks.run_worker_loop('lease-a', 'operator-a', stop)
    assert calls == [('lease-a', 'operator-a')] * 2
    assert stop.delays == [2.0]


def test_model_claim_failure_is_retried_without_busy_polling(monkeypatch):
    stop = ControlledStop(stop_after_waits=12)
    calls = []
    monkeypatch.setattr(estimate_tasks, 'process_next_estimate', lambda *args: False)

    def model(*args):
        calls.append(args)
        raise OperationalError('claim model', {}, ConnectionError('offline'))

    monkeypatch.setattr('worker.model_tasks.process_next_model_task', model)
    estimate_tasks.run_worker_loop('lease-a', 'operator-a', stop)
    assert len(calls) == 12
    assert stop.delays[:3] == [2.0, 3.0, 4.5]
    assert stop.delays[-1] == 30.0


def test_continuous_estimates_do_not_starve_model_queue(monkeypatch):
    stop = ControlledStop()
    calls = []
    monkeypatch.setattr(estimate_tasks, 'process_next_estimate', lambda *args: True)

    def model(*args):
        calls.append(args)
        stop.set()
        return True

    monkeypatch.setattr('worker.model_tasks.process_next_model_task', model)
    estimate_tasks.run_worker_loop('lease-a', 'operator-a', stop)
    assert calls == [('lease-a', 'operator-a')]
    assert stop.delays == []


def test_unexpected_failure_in_job_finalization_does_not_end_loop(monkeypatch):
    stop = ControlledStop(stop_after_waits=1)

    def estimate(*args):
        raise RuntimeError('failed to record job error')

    monkeypatch.setattr(estimate_tasks, 'process_next_estimate', estimate)
    estimate_tasks.run_worker_loop('lease-a', 'operator-a', stop)
    assert stop.delays == [2.0]


def test_real_claim_path_is_safe_when_connection_is_unavailable(monkeypatch):
    stop = ControlledStop(stop_after_waits=1)

    @contextmanager
    def offline():
        raise OperationalError('connect', {}, ConnectionError('offline'))
        yield  # pragma: no cover

    monkeypatch.setattr(estimate_tasks, 'session_scope', offline)
    estimate_tasks.run_worker_loop('lease-a', 'operator-a', stop)
    assert stop.delays == [2.0]


def test_continuously_busy_estimate_and_model_queues_do_not_starve_calibration(monkeypatch):
    stop = ControlledStop()
    calls = []
    monkeypatch.setattr(estimate_tasks, 'process_next_estimate', lambda *args: True)
    monkeypatch.setattr('worker.model_tasks.process_next_model_task', lambda *args: True)

    def calibration(*args):
        calls.append(args)
        stop.set()
        return True

    monkeypatch.setattr('worker.calibration_tasks.process_next_calibration_task', calibration)
    estimate_tasks.run_worker_loop('lease-a', 'operator-a', stop)
    assert calls == [('lease-a', 'operator-a')]


def test_calibration_claim_outage_uses_bounded_retry(monkeypatch):
    stop = ControlledStop(stop_after_waits=2)
    monkeypatch.setattr(estimate_tasks, 'process_next_estimate', lambda *args: False)
    monkeypatch.setattr('worker.model_tasks.process_next_model_task', lambda *args: False)

    def calibration(*args):
        raise OperationalError('claim calibration', {}, ConnectionError('offline'))

    monkeypatch.setattr('worker.calibration_tasks.process_next_calibration_task', calibration)
    estimate_tasks.run_worker_loop('lease-a', 'operator-a', stop)
    assert stop.delays == [2.0, 3.0]
