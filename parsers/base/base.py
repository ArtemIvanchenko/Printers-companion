from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from domain.enums.common import FileRole, SourceFileFamily
from domain.schemas.parsing import ParseDiagnosticRecord, ParseResult


class ParserContext(BaseModel):
    profile_id: str | None = None
    profile_version: str | None = None
    source_file_id: str | None = None
    session_id: str | None = None
    session_date_hint: str | None = None
    signal_mappings: dict[str, dict[str, Any]] = Field(default_factory=dict)
    options: dict[str, Any] = Field(default_factory=dict)


class BaseParser(ABC):
    name: str = "base"
    version: str = "0.1.0"
    file_family: SourceFileFamily = SourceFileFamily.unsupported
    role: FileRole = FileRole.unknown

    def can_parse(self, file_family: SourceFileFamily) -> bool:
        return file_family == self.file_family

    @abstractmethod
    def parse(self, path: Path, context: ParserContext) -> ParseResult:
        raise NotImplementedError

    def empty_result(self, path: Path, context: ParserContext) -> ParseResult:
        return ParseResult(
            parser_name=self.name,
            parser_version=self.version,
            profile_id=context.profile_id,
            file_family=self.file_family,
            role=self.role,
            diagnostics=[
                ParseDiagnosticRecord(
                    severity="info",
                    code="empty_file",
                    message=f"{path.name} is empty; preserved as data quality evidence.",
                )
            ],
            data_quality=["empty"],
            metadata={"file_name": path.name},
        )

