import hashlib
import logging
import time
from pathlib import Path
from threading import Lock

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


class CandidateTracker:
    """Remember the last *version* reported for each local import batch.

    A path alone is not an identity: operators routinely reuse the incoming
    directory, and exporters may replace a ZIP/folder under the same name.  A
    permanent ``set[path]`` therefore suppresses every later batch.  The
    metadata signature is intentionally cheap (no full hashing of multi-GB log
    files); the durable import service still calculates content checksums.

    Watchdog callbacks run on their observer thread while the periodic scan
    runs on the watcher thread, so reservation is protected by a lock.
    """

    def __init__(self) -> None:
        self._signatures: dict[str, str] = {}
        self._lock = Lock()

    def reserve(self, path: Path, signature: str) -> bool:
        key = str(path.resolve(strict=False))
        with self._lock:
            if self._signatures.get(key) == signature:
                return False
            self._signatures[key] = signature
            return True

    def release(self, path: Path, signature: str) -> None:
        """Allow a failed notification to retry, without erasing a newer one."""
        key = str(path.resolve(strict=False))
        with self._lock:
            if self._signatures.get(key) == signature:
                self._signatures.pop(key, None)


def candidate_signature(path: Path, incoming_path: Path) -> str | None:
    """Return a cheap signature that changes when this local batch changes.

    Loose logs form one batch rooted at ``incoming_path``; only immediate
    ``*.log`` children belong to that signature, otherwise an unrelated ZIP or
    export folder would spuriously re-notify the loose-log batch.  Dedicated
    folders are walked recursively so replacing a file inside the same folder
    is detected by the periodic scan.
    """
    try:
        if path == incoming_path:
            files = sorted(
                child
                for child in path.iterdir()
                if child.is_file()
                and not child.name.startswith(".")
                and child.suffix.lower() == ".log"
            )
            if not files:
                return None
            root = path
        elif path.is_file():
            files = [path]
            root = path.parent
        elif path.is_dir():
            files = sorted(child for child in path.rglob("*") if child.is_file())
            root = path
        else:
            return None

        digest = hashlib.sha256()
        # The shared incoming directory is only a logical container for loose
        # logs.  Its metadata also changes when an unrelated ZIP or export
        # folder is added, which must not manufacture a second loose-log
        # notification.  Dedicated file/folder batches do include their root
        # identity so replacing an empty folder at the same path is visible.
        if path != incoming_path:
            root_stat = path.stat()
            digest.update(
                (
                    f"root\0{root_stat.st_mtime_ns}\0{root_stat.st_ctime_ns}"
                    f"\0{root_stat.st_ino}\n"
                ).encode()
            )
        for child in files:
            stat = child.stat()
            relative = child.relative_to(root).as_posix()
            digest.update(
                (
                    f"{relative}\0{stat.st_size}\0{stat.st_mtime_ns}"
                    f"\0{stat.st_ctime_ns}\0{stat.st_ino}\n"
                ).encode("utf-8", errors="surrogateescape")
            )
        return digest.hexdigest()
    except OSError:
        # Copy/rename may race this metadata walk. The 5-second scan will retry
        # once the filesystem has settled; do not reserve an incomplete state.
        return None


def is_import_candidate(path: Path) -> bool:
    if path.name.startswith("."):
        return False
    if path.is_dir() and path.name != "incoming":
        return True
    return path.suffix.lower() in (".zip", ".log")


def import_batch_for(path: Path, incoming_path: Path) -> Path | None:
    """Map a filesystem event to a complete operator-confirmable batch.

    A copied directory or ZIP is already a batch. Loose ``*.log`` files are
    complementary parts of one printer export, so the watched parent is the
    batch and the stability check waits until copying has stopped.
    """
    if path.name.startswith("."):
        return None
    if path.is_dir():
        return path if path != incoming_path else incoming_path
    if path.suffix.lower() == ".zip":
        return path
    if path.suffix.lower() == ".log":
        return incoming_path
    return None


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
    """Auto-confirm only when the installation explicitly disables approval."""
    settings = get_settings()
    if settings.require_operator_import_confirmation:
        logger.info("Import awaits operator confirmation")
        return
    import_job_id = response.get("job", {}).get("import_job_id")
    if not import_job_id:
        return
    url = f"{settings.internal_api_url.rstrip('/')}/agent/import-callback"
    headers = {"X-API-Token": settings.agent_api_token}
    callback_data = f"import:{import_job_id}:confirm"
    try:
        with httpx.Client(timeout=20) as client:
            client.post(url, json={"callback_data": callback_data, "actor": "watcher"}, headers=headers)
        logger.info("Auto-confirmed import job %s", import_job_id)
    except Exception as exc:
        logger.error("Failed to auto-confirm import job %s: %s", import_job_id, exc)


def _handle_candidate(path: Path, incoming_path: Path, tracker: CandidateTracker) -> None:
    candidate = import_batch_for(path, incoming_path)
    if candidate is None:
        return
    signature = candidate_signature(candidate, incoming_path)
    if signature is None or not tracker.reserve(candidate, signature):
        return
    logger.info("Detected import candidate: %s", candidate)
    try:
        resp = notify_import_detected(candidate)
        auto_confirm_import(resp)
    except Exception as exc:
        logger.error("Failed to notify about import candidate %s: %s", candidate, exc)
        tracker.release(candidate, signature)


def scan_existing_candidates(incoming_path: Path, tracker: CandidateTracker) -> None:
    if not incoming_path.exists():
        logger.warning("Incoming path does not exist: %s", incoming_path)
        return
    try:
        candidates = {
            candidate
            for path in sorted(incoming_path.iterdir())
            if (candidate := import_batch_for(path, incoming_path)) is not None
        }
        for path in sorted(candidates):
            _handle_candidate(path, incoming_path, tracker)
    except Exception as exc:
        logger.error("Error scanning incoming path %s: %s", incoming_path, exc)


def run_polling(incoming_path: Path) -> None:
    tracker = CandidateTracker()
    while True:
        scan_existing_candidates(incoming_path, tracker)
        time.sleep(5)


def run_filesystem_events(incoming_path: Path) -> None:
    if Observer is None or FileSystemEventHandler is None:
        logger.warning("watchdog is unavailable; falling back to polling")
        run_polling(incoming_path)
        return

    tracker = CandidateTracker()

    class Handler(FileSystemEventHandler):
        def on_created(self, event) -> None:  # type: ignore[no-untyped-def]
            _handle(Path(event.src_path))

        def on_moved(self, event) -> None:  # type: ignore[no-untyped-def]
            _handle(Path(event.dest_path))

        def on_modified(self, event) -> None:  # type: ignore[no-untyped-def]
            # Parent-directory metadata changes accompany every child create;
            # the child event already identifies the correct batch. File
            # modifications matter because exporters often overwrite a ZIP or
            # loose log without creating a new path.
            if not getattr(event, "is_directory", False):
                _handle(Path(event.src_path))

    def _handle(path: Path) -> None:
        _handle_candidate(path, incoming_path, tracker)

    scan_existing_candidates(incoming_path, tracker)
    observer = Observer()
    observer.schedule(Handler(), str(incoming_path), recursive=False)
    observer.start()
    try:
        while True:
            scan_existing_candidates(incoming_path, tracker)
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
