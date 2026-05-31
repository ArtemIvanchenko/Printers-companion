import logging
import time
from pathlib import Path

import httpx
from tenacity import (
    after_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from core.config.settings import get_settings
from core.logging.config import configure_logging
from core.preflight import run_preflight, exit_on_failure

try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
except Exception:  # pragma: no cover - watchdog is optional in unit-test environments
    FileSystemEventHandler = None
    Observer = None


logger = logging.getLogger(__name__)


def is_import_candidate(path: Path) -> bool:
    if path.name.startswith("."):
        return False
    if path.is_dir() and path.name != "incoming":
        return True
    return path.suffix.lower() in (".zip", ".log")


@retry(
    retry=retry_if_exception_type((httpx.HTTPError, TimeoutError, ConnectionError)),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    stop=stop_after_attempt(5),
    after=after_log(logger, logging.WARNING),
    reraise=True,
)
def notify_import_detected(path: Path) -> dict:
    settings = get_settings()
    url = f"{settings.internal_api_url.rstrip('/')}/agent/import-detected"
    headers = {"X-API-Token": settings.agent_api_token}
    with httpx.Client(timeout=20) as client:
        response = client.post(url, json={"source_path": str(path)}, headers=headers)
        response.raise_for_status()
        return response.json()


def auto_confirm_import(response: dict) -> None:
    """Auto-confirm detected import so worker processes it immediately."""
    import_job_id = response.get("job", {}).get("import_job_id")
    if not import_job_id:
        return
    settings = get_settings()
    url = f"{settings.internal_api_url.rstrip('/')}/agent/import-callback"
    headers = {"X-API-Token": settings.agent_api_token}
    callback_data = f"import:{import_job_id}:confirm"
    try:
        with httpx.Client(timeout=20) as client:
            client.post(url, json={"callback_data": callback_data, "actor": "watcher"}, headers=headers)
        logger.info("Auto-confirmed import job %s", import_job_id)
    except Exception as exc:
        logger.error("Failed to auto-confirm import job %s: %s", import_job_id, exc)


def scan_existing_candidates(incoming_path: Path, seen: set[str]) -> None:
    if not incoming_path.exists():
        logger.warning("Incoming path does not exist: %s", incoming_path)
        return
    try:
        for path in sorted(incoming_path.iterdir()):
            key = str(path.resolve())
            if key in seen or not is_import_candidate(path):
                continue
            seen.add(key)
            logger.info("Detected import candidate: %s", path)
            try:
                resp = notify_import_detected(path)
                auto_confirm_import(resp)
            except Exception as exc:
                logger.error("Failed to notify about import candidate %s: %s", path, exc)
                # Don't mark as seen if notification failed, so we can retry later
                seen.discard(key)
    except Exception as exc:
        logger.error("Error scanning incoming path %s: %s", incoming_path, exc)


def run_polling(incoming_path: Path) -> None:
    seen: set[str] = set()
    while True:
        scan_existing_candidates(incoming_path, seen)
        time.sleep(5)


def run_filesystem_events(incoming_path: Path) -> None:
    if Observer is None or FileSystemEventHandler is None:
        logger.warning("watchdog is unavailable; falling back to polling")
        run_polling(incoming_path)
        return

    seen: set[str] = set()

    class Handler(FileSystemEventHandler):
        def on_created(self, event) -> None:  # type: ignore[no-untyped-def]
            _handle(Path(event.src_path))

        def on_moved(self, event) -> None:  # type: ignore[no-untyped-def]
            _handle(Path(event.dest_path))

    def _handle(path: Path) -> None:
        key = str(path.resolve())
        if key in seen or not is_import_candidate(path):
            return
        seen.add(key)
        logger.info("Filesystem event detected import candidate: %s", path)
        try:
            resp = notify_import_detected(path)
            auto_confirm_import(resp)
        except Exception as exc:
            logger.error("Failed to handle import candidate %s: %s", path, exc)

    scan_existing_candidates(incoming_path, seen)
    observer = Observer()
    observer.schedule(Handler(), str(incoming_path), recursive=False)
    observer.start()
    try:
        while True:
            scan_existing_candidates(incoming_path, seen)
            time.sleep(5)
    finally:
        observer.stop()
        observer.join()


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    report = run_preflight(settings, component="watcher")
    exit_on_failure(report)
    for warn in report.warnings:
        logger.warning("PREFLIGHT: %s", warn)
    incoming_path = Path(settings.incoming_path)
    logger.info(
        "Watcher starting: path=%s mode=%s require_confirmation=%s",
        incoming_path,
        settings.watch_mode,
        settings.require_operator_import_confirmation,
    )
    if settings.watch_mode == "filesystem_events":
        run_filesystem_events(incoming_path)
    else:
        run_polling(incoming_path)


if __name__ == "__main__":
    main()
