"""Crash-safe, workstation-local outbox for files waiting for the NAS.

The shared PostgreSQL database remains the only catalogue and MinIO remains the
only long-term blob store.  This module is deliberately only a local transport
buffer: it never performs analytics and it never attempts to merge card data.

Each operation lives in one directory and moves atomically between ``pending``,
``processing`` and ``failed``.  The payload is copied and SHA-256 checked before
the directory becomes visible to a consumer.  A deterministic operation id
makes retrying the same record/content pair idempotent on one workstation; the
database's unique ``(record_id, checksum)`` index is the cross-workstation fence.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Iterator

try:  # Docker/Linux and macOS development
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - native Windows development only
    _fcntl = None
    import msvcrt as _msvcrt


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_OPERATION_ID = _SHA256
_STATES = ("pending", "processing", "failed")
_THREAD_LOCKS: dict[str, threading.RLock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


class OutboxFullError(RuntimeError):
    """The configured local safety buffer has no room for another payload."""


class OutboxIntegrityError(RuntimeError):
    """A queued payload does not match the immutable manifest."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_time(value: object) -> float:
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return 0.0


class LocalNasOutbox:
    """Small file-system queue shared by this PC's local containers only."""

    def __init__(
        self,
        root: str | Path,
        *,
        max_bytes: int,
        retry_min_seconds: int = 5,
        retry_max_seconds: int = 300,
        claim_seconds: int = 900,
    ) -> None:
        self.root = Path(root)
        self.max_bytes = max(1, int(max_bytes))
        self.retry_min_seconds = max(1, int(retry_min_seconds))
        self.retry_max_seconds = max(self.retry_min_seconds, int(retry_max_seconds))
        self.claim_seconds = max(60, int(claim_seconds))
        lock_key = str(self.root.resolve())
        with _THREAD_LOCKS_GUARD:
            self._thread_lock = _THREAD_LOCKS.setdefault(lock_key, threading.RLock())
        self._ensure_layout()

    @classmethod
    def from_settings(cls, settings: object) -> "LocalNasOutbox":
        return cls(
            getattr(settings, "nas_outbox_path"),
            max_bytes=int(getattr(settings, "nas_outbox_max_bytes")),
            retry_min_seconds=int(getattr(settings, "nas_sync_retry_min_seconds")),
            retry_max_seconds=int(getattr(settings, "nas_sync_retry_max_seconds")),
            claim_seconds=int(getattr(settings, "nas_sync_claim_seconds")),
        )

    def _ensure_layout(self) -> None:
        root_existed = self.root.exists()
        self.root.mkdir(parents=True, exist_ok=True)
        created_state_dir = False
        for name in (*_STATES, "completed"):
            state_dir = self.root / name
            if not state_dir.exists():
                state_dir.mkdir(exist_ok=True)
                created_state_dir = True
        if not root_existed:
            self._fsync_directory(self.root.parent)
        if created_state_dir:
            self._fsync_directory(self.root)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        """Persist a rename/delete in its parent directory on POSIX."""
        if os.name == "nt":  # pragma: no cover - Windows has no directory fsync
            return
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @classmethod
    def _replace(cls, source: Path, destination: Path) -> None:
        os.replace(source, destination)
        cls._fsync_directory(destination.parent)
        if source.parent != destination.parent:
            cls._fsync_directory(source.parent)

    def _cleanup_completed_payloads_locked(self) -> None:
        """Finish payload reclamation if power failed after receipt creation."""
        for receipt in (self.root / "completed").glob("*.json"):
            operation_id = receipt.stem
            if not _OPERATION_ID.fullmatch(operation_id):
                continue
            try:
                manifest = self._read_json(receipt)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if (
                manifest.get("operation_id") != operation_id
                or not manifest.get("completed_at")
                or not isinstance(manifest.get("result"), dict)
            ):
                continue
            for state in _STATES:
                leftover = self.root / state / operation_id
                if leftover.is_dir():
                    shutil.rmtree(leftover)
                    self._fsync_directory(leftover.parent)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self._thread_lock:
            self._ensure_layout()
            with (self.root / ".lock").open("a+b") as lock:
                if _fcntl is not None:
                    _fcntl.flock(lock.fileno(), _fcntl.LOCK_EX)
                else:  # pragma: no cover - native Windows development only
                    lock.seek(0)
                    if not lock.read(1):
                        lock.write(b"0")
                        lock.flush()
                    lock.seek(0)
                    _msvcrt.locking(lock.fileno(), _msvcrt.LK_LOCK, 1)
                try:
                    # A producer that dies during the private copy never exposes a
                    # pending item, but it can leave disk usage behind. Holding the
                    # global lock proves no live producer is using these dirs.
                    for staging in self.root.glob(".staging-*"):
                        if staging.is_dir():
                            shutil.rmtree(staging)
                    self._cleanup_completed_payloads_locked()
                    yield
                finally:
                    if _fcntl is not None:
                        _fcntl.flock(lock.fileno(), _fcntl.LOCK_UN)
                    else:  # pragma: no cover - native Windows development only
                        lock.seek(0)
                        _msvcrt.locking(lock.fileno(), _msvcrt.LK_UNLCK, 1)

    @staticmethod
    def _write_json(path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            LocalNasOutbox._replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise OutboxIntegrityError(f"Invalid outbox manifest: {path}")
        return value

    @staticmethod
    def _operation_id(owner_node_id: str, record_id: str, checksum: str) -> str:
        raw = f"print_attachment\0{owner_node_id}\0{record_id}\0{checksum}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _locate_locked(self, operation_id: str) -> tuple[str, Path] | None:
        if not _OPERATION_ID.fullmatch(operation_id):
            return None
        receipt = self.root / "completed" / f"{operation_id}.json"
        if receipt.exists():
            return "completed", receipt
        for state in _STATES:
            candidate = self.root / state / operation_id
            if candidate.is_dir():
                return state, candidate
        return None

    def _public(self, manifest: dict[str, Any], state: str) -> dict[str, Any]:
        return {
            "operation_id": manifest.get("operation_id"),
            "kind": manifest.get("kind"),
            "status": state,
            "record_id": manifest.get("record_id"),
            "file_name": manifest.get("file_name"),
            "file_type": manifest.get("file_type"),
            "checksum": manifest.get("checksum"),
            "size_bytes": manifest.get("size_bytes"),
            "attempts": int(manifest.get("attempts") or 0),
            "created_at": manifest.get("created_at"),
            "next_attempt_at": manifest.get("next_attempt_at"),
            "last_error": manifest.get("last_error"),
            "result": manifest.get("result") or {},
        }

    def get(self, operation_id: str) -> dict[str, Any] | None:
        with self._locked():
            located = self._locate_locked(operation_id)
            if located is None:
                return None
            state, path = located
            manifest_path = path if state == "completed" else path / "manifest.json"
            return self._public(self._read_json(manifest_path), state)

    def _used_bytes_locked(self) -> int:
        total = 0
        for state in _STATES:
            for item in (self.root / state).iterdir():
                payload = item / "payload"
                if payload.is_file():
                    try:
                        total += payload.stat().st_size
                    except FileNotFoundError:
                        pass
        return total

    @staticmethod
    def _copy_and_hash(source: BinaryIO, destination: Path) -> tuple[int, str]:
        digest = hashlib.sha256()
        size = 0
        with destination.open("xb") as sink:
            while chunk := source.read(1024 * 1024):
                sink.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            sink.flush()
            os.fsync(sink.fileno())
        return size, digest.hexdigest()

    def enqueue_attachment(
        self,
        source_path: str | Path,
        *,
        owner_node_id: str,
        record_id: str,
        file_name: str,
        file_type: str,
        bucket: str,
        checksum: str,
        size_bytes: int,
        content_type: str = "application/octet-stream",
        reopen_completed: bool = False,
    ) -> dict[str, Any]:
        """Durably enqueue one immutable print attachment.

        Returning an existing operation is intentional: browser retry, process
        retry and a duplicate local upload are the same logical operation.
        """
        checksum = checksum.lower()
        if not _SAFE_ID.fullmatch(owner_node_id) or not _SAFE_ID.fullmatch(record_id):
            raise ValueError("Unsafe owner/record identifier for NAS outbox")
        if not _SAFE_ID.fullmatch(bucket) or not _SHA256.fullmatch(checksum):
            raise ValueError("Invalid bucket or SHA-256 for NAS outbox")
        if not file_name or len(file_name) > 300 or len(file_type) > 40:
            raise ValueError("Invalid attachment metadata for NAS outbox")

        operation_id = self._operation_id(owner_node_id, record_id, checksum)
        with self._locked():
            located = self._locate_locked(operation_id)
            if located is not None:
                state, path = located
                if state == "completed" and reopen_completed:
                    # PostgreSQL is authoritative. A receipt without the row can
                    # happen after a database restore; rebuild the transport
                    # operation from the caller's still-available source.
                    path.unlink(missing_ok=True)
                    located = None
                else:
                    manifest_path = path if state == "completed" else path / "manifest.json"
                    return self._public(self._read_json(manifest_path), state)
            expected_size = int(size_bytes)
            if expected_size <= 0:
                raise OutboxIntegrityError("Cannot enqueue an empty attachment")
            if self._used_bytes_locked() + expected_size > self.max_bytes:
                raise OutboxFullError(
                    f"Local NAS outbox limit exceeded ({self.max_bytes} bytes)"
                )

            staging = self.root / f".staging-{operation_id}-{uuid.uuid4().hex}"
            staging.mkdir(mode=0o700)
            try:
                with Path(source_path).open("rb") as source:
                    copied_size, copied_checksum = self._copy_and_hash(
                        source, staging / "payload"
                    )
                if copied_size != expected_size or copied_checksum != checksum:
                    raise OutboxIntegrityError(
                        "Staged attachment changed while entering the NAS outbox"
                    )
                file_id = f"prf_sync_{operation_id[:48]}"
                now = _utc_now()
                manifest = {
                    "schema_version": 1,
                    "operation_id": operation_id,
                    "kind": "print_attachment",
                    "owner_node_id": owner_node_id,
                    "record_id": record_id,
                    "file_id": file_id,
                    "file_name": file_name,
                    "file_type": file_type,
                    "bucket": bucket,
                    "object_name": f"{record_id}/{file_id}_{checksum}",
                    "checksum": checksum,
                    "size_bytes": copied_size,
                    "content_type": content_type,
                    "attempts": 0,
                    "created_at": now,
                    "updated_at": now,
                    "next_attempt_at": now,
                    "last_error": None,
                }
                self._write_json(staging / "manifest.json", manifest)
                self._replace(staging, self.root / "pending" / operation_id)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
            return self._public(manifest, "pending")

    def _recover_expired_locked(self) -> None:
        now = time.time()
        for item in list((self.root / "processing").iterdir()):
            if not item.is_dir():
                continue
            try:
                manifest = self._read_json(item / "manifest.json")
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                self._quarantine_corrupt_locked(item, exc)
                continue
            claimed_at = _parse_time(manifest.get("claimed_at"))
            if claimed_at and now - claimed_at < self.claim_seconds:
                continue
            manifest["claimed_at"] = None
            manifest["updated_at"] = _utc_now()
            manifest["last_error"] = "Local sync process stopped before completion"
            self._write_json(item / "manifest.json", manifest)
            self._replace(item, self.root / "pending" / item.name)

    def _quarantine_corrupt_locked(self, item: Path, error: Exception) -> None:
        """Preserve bytes but make a malformed operation visible as failed."""
        payload = item / "payload"
        manifest = {
            "schema_version": 1,
            "operation_id": item.name,
            "kind": "corrupt_manifest",
            "size_bytes": payload.stat().st_size if payload.is_file() else 0,
            "attempts": 0,
            "created_at": _utc_now(),
            "updated_at": _utc_now(),
            "last_error": f"Outbox manifest is corrupt: {error}"[:1000],
        }
        self._write_json(item / "manifest.json", manifest)
        target = self.root / "failed" / item.name
        if item != target:
            self._replace(item, target)

    def claim(self, operation_id: str | None = None) -> dict[str, Any] | None:
        """Atomically lease one due item to the calling local process."""
        if operation_id is not None and not _OPERATION_ID.fullmatch(operation_id):
            return None
        with self._locked():
            self._recover_expired_locked()
            pending = self.root / "pending"
            candidates = (
                [pending / operation_id]
                if operation_id is not None
                else sorted(pending.iterdir(), key=lambda path: path.name)
            )
            now = time.time()
            for item in candidates:
                if not item.is_dir():
                    continue
                try:
                    manifest = self._read_json(item / "manifest.json")
                except (OSError, ValueError, json.JSONDecodeError) as exc:
                    self._quarantine_corrupt_locked(item, exc)
                    continue
                if (
                    not _OPERATION_ID.fullmatch(item.name)
                    or manifest.get("operation_id") != item.name
                ):
                    self._quarantine_corrupt_locked(
                        item,
                        OutboxIntegrityError("Manifest operation id does not match its directory"),
                    )
                    continue
                if _parse_time(manifest.get("next_attempt_at")) > now:
                    continue
                target = self.root / "processing" / item.name
                self._replace(item, target)
                manifest["claimed_at"] = _utc_now()
                manifest["updated_at"] = manifest["claimed_at"]
                self._write_json(target / "manifest.json", manifest)
                return {**manifest, "payload_path": str(target / "payload")}
        return None

    def verify_claimed(self, item: dict[str, Any]) -> Path:
        payload = Path(str(item["payload_path"]))
        digest = hashlib.sha256()
        size = 0
        try:
            with payload.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
                    size += len(chunk)
        except OSError as exc:
            raise OutboxIntegrityError(f"Queued payload is unreadable: {exc}") from exc
        if size != int(item["size_bytes"]) or digest.hexdigest() != item["checksum"]:
            raise OutboxIntegrityError("Queued payload checksum/size mismatch")
        return payload

    def release(self, operation_id: str, error: str) -> None:
        """Return a claimed item to pending with bounded exponential backoff."""
        if not _OPERATION_ID.fullmatch(operation_id):
            raise ValueError("Invalid NAS outbox operation id")
        with self._locked():
            item = self.root / "processing" / operation_id
            if not item.is_dir():
                return
            manifest = self._read_json(item / "manifest.json")
            attempts = int(manifest.get("attempts") or 0) + 1
            delay = min(
                self.retry_max_seconds,
                self.retry_min_seconds * (2 ** min(16, attempts - 1)),
            )
            manifest.update({
                "attempts": attempts,
                "claimed_at": None,
                "updated_at": _utc_now(),
                "next_attempt_at": datetime.fromtimestamp(
                    time.time() + delay, timezone.utc
                ).isoformat(),
                "last_error": str(error)[:1000],
            })
            self._write_json(item / "manifest.json", manifest)
            self._replace(item, self.root / "pending" / operation_id)

    def fail(self, operation_id: str, error: str) -> None:
        """Quarantine a permanent conflict/corrupt payload for operator review."""
        if not _OPERATION_ID.fullmatch(operation_id):
            raise ValueError("Invalid NAS outbox operation id")
        with self._locked():
            item = self.root / "processing" / operation_id
            if not item.is_dir():
                return
            manifest = self._read_json(item / "manifest.json")
            manifest.update({
                "claimed_at": None,
                "updated_at": _utc_now(),
                "last_error": str(error)[:1000],
            })
            self._write_json(item / "manifest.json", manifest)
            self._replace(item, self.root / "failed" / operation_id)

    def complete(self, operation_id: str, result: dict[str, Any]) -> None:
        """Write a small durable receipt, then reclaim the large local payload."""
        if not _OPERATION_ID.fullmatch(operation_id):
            raise ValueError("Invalid NAS outbox operation id")
        with self._locked():
            item = self.root / "processing" / operation_id
            if not item.is_dir():
                return
            manifest = self._read_json(item / "manifest.json")
            manifest.update({
                "claimed_at": None,
                "updated_at": _utc_now(),
                "completed_at": _utc_now(),
                "last_error": None,
                "result": result,
            })
            self._write_json(
                self.root / "completed" / f"{operation_id}.json",
                manifest,
            )
            shutil.rmtree(item)
            self._fsync_directory(item.parent)

    def retry_failed(self, operation_id: str) -> bool:
        if not _OPERATION_ID.fullmatch(operation_id):
            return False
        with self._locked():
            item = self.root / "failed" / operation_id
            if not item.is_dir():
                return False
            manifest = self._read_json(item / "manifest.json")
            manifest.update({
                "attempts": 0,
                "updated_at": _utc_now(),
                "next_attempt_at": _utc_now(),
                "last_error": None,
            })
            self._write_json(item / "manifest.json", manifest)
            self._replace(item, self.root / "pending" / operation_id)
            return True

    def status(self) -> dict[str, Any]:
        with self._locked():
            self._recover_expired_locked()
            counts: dict[str, int] = {}
            oldest: str | None = None
            failures: list[dict[str, Any]] = []
            bytes_waiting = 0
            for state in _STATES:
                items = [path for path in (self.root / state).iterdir() if path.is_dir()]
                counts[state] = len(items)
                for item in items:
                    try:
                        manifest = self._read_json(item / "manifest.json")
                    except (OSError, ValueError, json.JSONDecodeError):
                        continue
                    created = str(manifest.get("created_at") or "")
                    if created and (oldest is None or created < oldest):
                        oldest = created
                    bytes_waiting += int(manifest.get("size_bytes") or 0)
                    if state == "failed" and len(failures) < 20:
                        failures.append(self._public(manifest, state))
            counts["completed"] = sum(
                1 for path in (self.root / "completed").iterdir() if path.is_file()
            )
            return {
                "counts": counts,
                "bytes_waiting": bytes_waiting,
                "capacity_bytes": self.max_bytes,
                "oldest_waiting_at": oldest,
                "failed_items": failures,
            }
