from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from core.utils.files import safe_relative, sha256_file
from domain.enums.common import DataQualityStatus
from domain.schemas.parsing import FileClassification, ParseResult
from domain.services.file_classifier import classify_file
from parsers.base.base import ParserContext
from parsers.base.registry import ParserRegistry
from parsers.common.encoding import estimate_encoding, is_probably_binary
from profiles.base.profile import PrinterProfilePlugin


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
    def __init__(self, registry: ParserRegistry, profile: PrinterProfilePlugin | None = None) -> None:
        self.registry = registry
        self.profile = profile
        self.profile_id = profile.profile_id if profile else None

    def scan(self, root: Path) -> IngestionResult:
        result = IngestionResult(root=str(root))
        if not root.exists():
            result.diagnostics.append({"severity": "error", "code": "root_missing", "path": str(root)})
            return result
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            try:
                result.files.append(self._inspect_file(path, root))
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
            item.parse_result = self.registry.parse(path, item.classification.family, context)
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

