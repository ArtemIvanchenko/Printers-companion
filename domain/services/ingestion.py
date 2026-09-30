import logging
from datetime import datetime, timezone
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from core.utils.files import iter_source_files, safe_relative, sha256_file
from domain.enums.common import DataQualityStatus
from domain.schemas.parsing import FileClassification, ParseResult
from domain.services.file_classifier import classify_file
from parsers.base.base import ParserContext
from parsers.base.registry import ParserRegistry
from parsers.common.encoding import estimate_encoding, is_probably_binary
from profiles.base.profile import PrinterProfilePlugin

logger = logging.getLogger(__name__)


class IngestedFile(BaseModel):
    path: str
    relative_path: str
    classification: FileClassification
    checksum: str
    size_bytes: int
    encoding: str | None = None
    data_quality_status: DataQualityStatus
    mtime: datetime
    object_uri: str | None = None
    parse_result: ParseResult | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class IngestionResult(BaseModel):
    root: str
    files: list[IngestedFile] = Field(default_factory=list)
    skipped: list[dict[str, Any]] = Field(default_factory=list)
    diagnostics: list[dict[str, Any]] = Field(default_factory=list)


class IngestionService:
    # Only OS metadata is universally irrelevant. Machine source selection
    # belongs to the selected profile; burn logs carry independent layer times.
    SKIP_PATTERNS: tuple[str, ...] = (
        "._*",            # macOS AppleDouble metadata files
        ".DS_Store",      # Finder folder metadata — ingested as a "session" otherwise
        "Thumbs.db",      # Windows Explorer thumbnail cache
        "desktop.ini",
    )
    def _should_skip(self, path: Path) -> str | None:
        """Return a skip reason string if this file should not be ingested, else None."""
        name = path.name
        patterns = self.SKIP_PATTERNS + tuple(getattr(self.profile, "excluded_source_patterns", ()))
        for pattern in patterns:
            if fnmatch(name, pattern):
                return f"source excluded by metadata/profile policy: '{pattern}'"
        for pattern, limit in getattr(self.profile, "source_size_limits", {}).items():
            if fnmatch(name, pattern) and path.stat().st_size > limit:
                return f"profile source size limit: '{pattern}' exceeds {limit} bytes"
        return None

    def __init__(self, registry: ParserRegistry, profile: PrinterProfilePlugin | None = None) -> None:
        self.registry = registry
        self.profile = profile
        self.profile_id = profile.profile_id if profile else None

    def scan(self, root: Path) -> IngestionResult:
        result = IngestionResult(root=str(root))
        if not root.exists():
            result.diagnostics.append({"severity": "error", "code": "root_missing", "path": str(root)})
            return result
        relative_root = root.parent if root.is_file() else root
        for path in sorted(iter_source_files(root)):
            try:
                if not path.is_file():
                    continue
                # _should_skip may stat() the file; a file deleted/rotated mid-scan
                # would raise OSError here — keep it inside the guard so one
                # vanishing file can't abort the whole scan.
                skip_reason = self._should_skip(path)
                if skip_reason:
                    result.skipped.append({"path": str(path), "reason": skip_reason})
                    continue
                result.files.append(self._inspect_file(path, relative_root))
            except OSError as exc:
                result.skipped.append({"path": str(path), "reason": str(exc)})
        return result

    def parse(self, root: Path) -> IngestionResult:
        result = self.scan(root)
        if self.profile is None:
            return result
        for item in result.files:
            path = Path(item.path)
            context = ParserContext(
                profile_id=self.profile.profile_id,
                profile_version=self.profile.version,
                signal_mappings=self.profile.signal_mappings,
            )
            try:
                item.parse_result = self.registry.parse(path, item.classification.family, context)
            except Exception as exc:  # noqa: BLE001 - one bad file must not kill the batch
                item.parse_result = None
                result.diagnostics.append({
                    "severity": "error", "code": "parse_failed",
                    "path": str(path), "detail": str(exc),
                })
                logger.warning("Parsing failed for %s: %s", path, exc)
        return result

    def _inspect_file(self, path: Path, root: Path) -> IngestedFile:
        stat = path.stat()
        size = stat.st_size
        classification = classify_file(path)
        data_quality = DataQualityStatus.ok
        encoding: str | None = None
        if size == 0:
            data_quality = DataQualityStatus.zero_byte
        elif is_probably_binary(path):
            data_quality = DataQualityStatus.binary_or_unknown
        else:
            encoding = estimate_encoding(path)
        return IngestedFile(
            path=str(path),
            relative_path=safe_relative(path, root),
            classification=classification,
            checksum=sha256_file(path),
            size_bytes=size,
            encoding=encoding,
            data_quality_status=data_quality,
            mtime=datetime.fromtimestamp(stat.st_mtime, timezone.utc),
            metadata={"raw_file_name": path.name},
        )
