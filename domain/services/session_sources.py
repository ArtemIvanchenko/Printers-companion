"""Local raw-source IO, independent of SQL and its caller's transaction.

New import batches are archived in full before analysis. The small time-log
mirror is an additional fast path; unavailable originals remain explicitly slim.
"""

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

from core.utils.files import sha256_file
from domain.enums.common import SourceFileFamily
from domain.services.compute_affinity import require_compute_owner
from domain.services.ingestion import IngestedFile

logger = logging.getLogger(__name__)

# Only compact calibration-critical logs get this additional session mirror.
# Large sensor/state sources remain in the immutable raw import archive.
_SHARED_LOG_FAMILIES = frozenset({SourceFileFamily.time_log})


def _shared_log_object_name(session_id: str, file_name: str) -> str:
    return f"{session_id}/{file_name}"


def mirror_logs_to_object_store(session_id: str, files: list[IngestedFile], *, immutable: bool = False) -> int:
    """Copy calibration-critical logs to a session-addressable fast path.

    Best-effort: object storage being down must never fail an import, since the
    on-disk copy is still the primary. Returns how many files were stored.
    """
    from storage.object_store.minio_client import ObjectStore

    candidates = [
        f for f in files
        if f.classification.family in _SHARED_LOG_FAMILIES and f.path and Path(f.path).exists()
    ]
    if not candidates:
        return 0
    try:
        store = ObjectStore()
        if not store.is_available():
            return 0
        bucket = store.settings.minio_bucket_raw
        for f in candidates:
            name = f.classification.file_name or Path(f.relative_path).name
            if immutable:
                # A discarded import must not replace another generation's
                # readable mirror. The URI is published only with its payload.
                uri = store.put_file_verified(
                    bucket, f"{session_id}/sha256/{f.checksum}/{name}", Path(f.path),
                    expected_sha256=f.checksum, expected_size=f.size_bytes,
                )
                f.metadata["shared_log_uri"] = uri
            else:
                store.put_file(bucket, _shared_log_object_name(session_id, name), Path(f.path))
    except Exception as exc:
        logger.warning("mirror_logs_to_object_store(%s) failed: %s", session_id, exc)
        return 0
    return len(candidates)


def fetch_shared_log(session_id: str, file_name: str, *, object_uri: str | None = None) -> Path | None:
    """Pull a mirrored log into a temp file so the parsers can read a path."""
    import tempfile
    from storage.object_store.minio_client import ObjectStore

    if (file_name in {"", ".", ".."} or session_id in {"", ".", ".."}
            or Path(file_name).name != file_name or Path(session_id).name != session_id):
        return None
    expected_hash = None
    try:
        store = ObjectStore()
        object_name = _shared_log_object_name(session_id, file_name)
        if object_uri:
            prefix = f"s3://{store.settings.minio_bucket_raw}/{session_id}/sha256/"
            if not object_uri.startswith(prefix):
                return None
            expected_hash, separator, stored_name = object_uri[len(prefix):].partition("/")
            if (not separator or stored_name != file_name or len(expected_hash) != 64
                    or any(char not in "0123456789abcdef" for char in expected_hash)):
                return None
            object_name = object_uri[len(f"s3://{store.settings.minio_bucket_raw}/"):]
        data = store.get_bytes(store.settings.minio_bucket_raw, object_name)
    except Exception:
        return None
    if not data:
        return None
    checksum = hashlib.sha256(data).hexdigest()
    if expected_hash is not None and checksum != expected_hash:
        raise ValueError(f"SHA-256 копии {file_name} на NAS не совпадает; нужен повторный импорт")
    tmp = Path(tempfile.gettempdir()) / "pc-shared-logs" / session_id / checksum
    tmp.mkdir(parents=True, exist_ok=True)
    path = tmp / file_name
    # Different generations never share a mutable cache path. Publish the local
    # file by rename so parallel readers cannot observe a partial download.
    import os
    with tempfile.NamedTemporaryFile(dir=tmp, delete=False) as staged:
        staged.write(data)
    try:
        os.replace(staged.name, path)
    finally:
        Path(staged.name).unlink(missing_ok=True)
    return path


def _source_signature(path: Path) -> tuple[int, int, int, int]:
    try:
        stat = path.stat()
    except OSError as exc:
        raise ValueError(f"Не удалось проверить источник {path.name}; нужен повторный импорт") from exc
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def _require_unchanged(path: Path, signature: tuple[int, int, int, int]) -> None:
    if _source_signature(path) != signature:
        raise ValueError(f"Источник {path.name} изменился во время чтения; нужен повторный импорт")


def _verified_source(file: IngestedFile, path: Path) -> tuple[int, int, int, int]:
    checksum = file.checksum.lower()
    if len(checksum) != 64 or any(char not in "0123456789abcdef" for char in checksum):
        raise ValueError(f"У источника {path.name} нет проверяемого SHA-256; нужен повторный импорт")
    signature = _source_signature(path)
    if signature[2] != file.size_bytes:
        raise ValueError(f"Размер источника {path.name} изменился; нужен повторный импорт")
    try:
        actual_checksum = sha256_file(path)
    except OSError as exc:
        raise ValueError(f"Не удалось проверить SHA-256 источника {path.name}; нужен повторный импорт") from exc
    _require_unchanged(path, signature)
    if actual_checksum != checksum:
        raise ValueError(f"SHA-256 источника {path.name} изменился; нужен повторный импорт")
    return signature


def rehydrate_parse_results(
    files: list[IngestedFile], session_id: str | None = None,
) -> list[IngestedFile]:
    """Re-parse files that were stored without parse_result (events stripped).

    Used by owner-local legacy repair/research, not production report reads or
    calibration. When the local time-log copy is missing, restore its NAS mirror.
    Sources available from neither remain slim; this does not claim completeness.
    """
    need: list[tuple[IngestedFile, Path]] = []
    for f in files:
        if f.parse_result is not None:
            continue
        if f.path and Path(f.path).exists():
            need.append((f, Path(f.path)))
        elif session_id and f.classification.family in _SHARED_LOG_FAMILIES:
            name = f.classification.file_name or Path(f.relative_path).name
            shared = fetch_shared_log(session_id, name, object_uri=f.metadata.get("shared_log_uri"))
            if shared:
                need.append((f, shared))
    if not need:
        return files
    # Validate the whole available batch before assigning any parser results.
    # A changed/unknown identity must escape, not look like a missing legacy log.
    verified = [(file, path, _verified_source(file, path)) for file, path in need]
    try:
        from parsers.base.base import ParserContext
        from profiles.m350.profile import build_registry, get_profile
        registry = build_registry()
        profile = get_profile()
    except Exception as exc:
        logger.warning("Rehydrate parse results failed: %s", exc)
        return files
    for file, path, signature in verified:
        _require_unchanged(path, signature)
        try:
            ctx = ParserContext(
                profile_id=profile.profile_id,
                profile_version=profile.version,
                signal_mappings=profile.signal_mappings,
            )
            parsed = registry.parse(path, file.classification.family, ctx)
        except Exception as exc:
            logger.warning("Rehydrate parse results failed: %s", exc)
            return files
        finally:
            # Rotation/growth after validation invalidates even a successful parse.
            _require_unchanged(path, signature)
        file.parse_result = parsed
    return files

@dataclass(frozen=True)
class SessionSources:
    session_id: str
    owner_node_id: str | None
    files: list[IngestedFile]


def sources_from_snapshot(session_id: str, snapshot: dict | None) -> SessionSources | None:
    """Validate detached SQL data; this does not open files or a connection."""
    if snapshot is None:
        return None
    return SessionSources(session_id, snapshot["owner_node_id"],
                          [IngestedFile.model_validate(item) for item in snapshot["files"]])


def read_session_sources(db, session_id: str) -> SessionSources | None:
    """Standalone read boundary; pass a clean session, never pending writes."""
    from storage.repositories.session_reads import SessionReadsRepository

    try:
        snapshot = SessionReadsRepository(db).sources_snapshot(session_id)
    finally:
        db.rollback()
    return sources_from_snapshot(session_id, snapshot)


def rehydrate_session_sources(sources: SessionSources, *, compute_node_id: str | None = None) -> list[IngestedFile]:
    """Owner-local raw reconstruction; call only after closing your read UoW."""
    if compute_node_id is None:
        from core.config.settings import get_settings
        compute_node_id = get_settings().compute_node_id
    require_compute_owner(entity_type="session", entity_id=sources.session_id,
                          origin_compute_node_id=sources.owner_node_id,
                          requested_compute_node_id=compute_node_id)
    return rehydrate_parse_results(sources.files, sources.session_id)
