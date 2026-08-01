"""PLAN_ACCURACY.md 2.4 — the estimate's heavy part runs in a separate OS
process outside test mode (api/routes/prints.py's _auto_estimate), not just a
same-process call dressed up to look like one. Every other estimate test in
the suite runs with APP_ENV=test, which takes the inline fallback on purpose
(a subprocess would not see a monkeypatched ObjectStore — see that branch's
docstring) — so none of them actually exercise the process boundary. This
file is the one that does.

Uses a print record with no STL attached: _compute_prediction_snapshot raises
its "no STL" HTTPException before ever touching ObjectStore, so this needs no
real MinIO to prove the subprocess mechanism itself works.
"""
import asyncio
from datetime import datetime, timezone

import pytest

from domain.models.prints import PrintRecord
from storage.db.session import SessionLocal


def _make_bare_record(record_id: str) -> None:
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        db.merge(PrintRecord(record_id=record_id, name="без STL (subprocess test)",
                              created_at=now, updated_at=now))
        db.commit()


class TestRunEstimateInProcess:
    def test_exception_survives_the_process_boundary(self):
        """A direct ProcessPoolExecutor call — no _auto_estimate involved yet —
        proves _run_estimate_in_process is picklable, importable and runs to
        completion in a genuinely different process."""
        from concurrent.futures import ProcessPoolExecutor

        from api.routes.prints import _run_estimate_in_process

        _make_bare_record("pr_subproc_direct")
        with ProcessPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_run_estimate_in_process, "pr_subproc_direct")
            with pytest.raises(Exception) as exc_info:
                future.result(timeout=60)
        assert "не прикреплён STL" in str(exc_info.value)


class TestAutoEstimateProcessPoolBranch:
    def test_routes_through_the_real_pool_and_survives_the_child_exception(self, monkeypatch, caplog):
        """Forces _auto_estimate's non-test branch (real env here is
        APP_ENV=test, like the rest of the suite) and proves it actually goes
        through _ESTIMATE_POOL rather than silently taking the inline path.

        _run_estimate_in_process itself is left untouched — wrapping it in a
        test-local closure would make it unpicklable (ProcessPoolExecutor can
        only pickle module-level callables by reference, see its own
        docstring), which defeats the point of testing the real pool. Instead
        this relies on the same "no STL" HTTPException used by the first test:
        seeing its exact message survive the child -> Future -> except
        HTTPException path is itself proof the child ran the real function and
        its result (here, an exception) crossed the process boundary intact.
        """
        import logging

        import api.routes.prints as prints_module

        class _NotTestSettings:
            app_env = "production"

        monkeypatch.setattr(prints_module, "get_settings", lambda: _NotTestSettings())

        record_id = "pr_subproc_auto"
        _make_bare_record(record_id)

        try:
            with caplog.at_level(logging.INFO, logger="api.routes.prints"):
                asyncio.run(prints_module._auto_estimate(record_id))  # must not raise: caught internally
            assert prints_module._ESTIMATE_POOL is not None, "the pool branch was never taken"
            assert any("не прикреплён STL" in r.message for r in caplog.records)
        finally:
            if prints_module._ESTIMATE_POOL is not None:
                prints_module._ESTIMATE_POOL.shutdown(wait=True)
                prints_module._ESTIMATE_POOL = None
