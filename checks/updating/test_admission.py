"""ASGI lifecycle and real shared OS locks, isolated from operator data."""
import asyncio
import os

import pytest

from core.maintenance import UpdateAdmissionMiddleware, drain_writes, write_admission


@pytest.fixture(autouse=True)
def local_barrier(tmp_path, monkeypatch):
    monkeypatch.setenv('OPERATOR_INSTANCE_FILE', str(tmp_path / 'instance-id'))
    return tmp_path / 'operator-update.json'


def test_new_writes_blocked_but_reads_still_available(local_barrier):
    async def application(scope, receive, send):
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'{}'})
    async def request(method):
        messages = []
        async def send(message):
            messages.append(message)
        await UpdateAdmissionMiddleware(application)({'type': 'http', 'method': method}, None, send)
        return messages[0]['status']
    local_barrier.write_text('{"token":"test"}')
    assert asyncio.run(request('POST')) == 503
    assert asyncio.run(request('GET')) == 200


@pytest.mark.skipif(os.name != 'posix', reason='Production update lock is in Linux Docker')
def test_marker_after_admission_waits_for_complete_request_including_background(local_barrier):
    async def scenario():
        async def application(scope, receive, send):
            await send({'type': 'http.response.start', 'status': 200})
            await send({'type': 'http.response.body', 'body': b'done'})
            local_barrier.write_text('{"token":"test"}')
            with pytest.raises(TimeoutError):
                drain_writes(timeout=0)
        async def send(message):
            pass
        await UpdateAdmissionMiddleware(application)({'type': 'http', 'method': 'POST'}, None, send)
        drain_writes(timeout=0)
    asyncio.run(scenario())


@pytest.mark.skipif(os.name != 'posix', reason='Production update lock is in Linux Docker')
def test_request_exception_releases_admission_lock(local_barrier):
    with pytest.raises(ValueError):
        with write_admission() as admitted:
            assert admitted
            local_barrier.write_text('{"token":"test"}')
            raise ValueError('cancelled request')
    drain_writes(timeout=0)
