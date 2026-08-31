"""Tiny lease heartbeat used while CPU-heavy work runs off the NAS."""

from __future__ import annotations

import logging
from collections.abc import Callable
from threading import Event, Thread


logger = logging.getLogger(__name__)


class LeaseHeartbeat:
    """Periodically renew a durable job lease in a short transaction.

    ``renew`` must open and close its own database unit of work.  The worker's
    expensive parsing/geometry code therefore never holds a PostgreSQL
    transaction while this helper contributes only one tiny update per
    interval.  A false result means the generation was lost permanently;
    transient connection errors are logged and retried until final fencing.
    """

    def __init__(
        self,
        renew: Callable[[], bool],
        *,
        description: str,
        interval_seconds: float = 300.0,
    ) -> None:
        self._renew = renew
        self._description = description
        self._interval_seconds = max(1.0, interval_seconds)
        self._stop = Event()
        self._lost = Event()
        self._thread = Thread(
            target=self._run,
            name=f"lease-heartbeat-{description}",
            daemon=True,
        )

    @property
    def lost(self) -> bool:
        return self._lost.is_set()

    def __enter__(self) -> LeaseHeartbeat:
        self._thread.start()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)

    def _run(self) -> None:
        while not self._stop.wait(self._interval_seconds):
            try:
                if self._renew():
                    continue
                self._lost.set()
                logger.warning("lease heartbeat lost ownership of %s", self._description)
                return
            except Exception:
                # A brief NAS outage must not kill a perfectly valid local
                # calculation. Finalization still performs an authoritative
                # generation check before writing any result.
                logger.exception("lease heartbeat failed for %s", self._description)
