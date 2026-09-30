import io
import json
from urllib.error import HTTPError

import pytest

from scripts.maintenance.desktop_runtime import check_local_readiness


@pytest.mark.parametrize('missing', ['database', 'schema', 'redis', 'minio', 'minio_buckets'])
def test_listening_api_with_unavailable_storage_is_not_ready(monkeypatch, missing):
    checks = dict.fromkeys(('database', 'schema', 'redis', 'minio', 'minio_buckets'), True)
    checks[missing] = False
    monkeypatch.setattr('urllib.request.urlopen', lambda *a, **k: io.BytesIO(json.dumps({
        'status': 'ready', 'checks': checks,
    }).encode()))
    with pytest.raises(RuntimeError, match='хранилище'):
        check_local_readiness()


def test_readiness_failure_never_falls_back_to_liveness(monkeypatch):
    def request(url, timeout):
        assert url.endswith('/health/ready')
        assert timeout <= 15
        raise HTTPError(url, 503, 'unavailable', {}, None)

    monkeypatch.setattr('urllib.request.urlopen', request)
    with pytest.raises(RuntimeError, match='не готово'):
        check_local_readiness()


def test_ready_requires_every_backing_service(monkeypatch):
    state = {'status': 'ready', 'checks': dict.fromkeys(('database', 'schema', 'redis', 'minio', 'minio_buckets'), True)}
    monkeypatch.setattr('urllib.request.urlopen', lambda *a, **k: io.BytesIO(json.dumps(state).encode()))
    assert check_local_readiness() == state


def test_older_api_cannot_confirm_new_checkout_is_running(monkeypatch):
    state = {'status': 'ready', 'checks': dict.fromkeys(
        ('database', 'schema', 'redis', 'minio', 'minio_buckets'), True),
        'build': {'git_sha': 'old-build'}}
    monkeypatch.setattr('urllib.request.urlopen', lambda *a, **k: io.BytesIO(json.dumps(state).encode()))
    with pytest.raises(RuntimeError, match='другой'):
        check_local_readiness(expected_git_sha='new-build')
    assert check_local_readiness(expected_git_sha='old-build') == state
