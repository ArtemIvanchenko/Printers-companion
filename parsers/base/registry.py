from pathlib import Path

from domain.enums.common import SourceFileFamily
from domain.schemas.parsing import ParseDiagnosticRecord, ParseResult
from parsers.base.base import BaseParser, ParserContext


class ParserRegistry:
    def __init__(self) -> None:
        self._parsers: dict[SourceFileFamily, BaseParser] = {}

    def register(self, parser: BaseParser) -> None:
        self._parsers[parser.file_family] = parser

    def get(self, file_family: SourceFileFamily) -> BaseParser | None:
        return self._parsers.get(file_family)

    def parse(self, path: Path, file_family: SourceFileFamily, context: ParserContext) -> ParseResult:
        parser = self.get(file_family)
        if parser is None:
            return ParseResult(
                parser_name="unsupported",
                parser_version="0.1.0",
                profile_id=context.profile_id,
                file_family=file_family,
                role="unknown",
                diagnostics=[
                    ParseDiagnosticRecord(
                        severity="warning",
                        code="unsupported_file_family",
                        message=f"No parser registered for {file_family}.",
                    )
                ],
                data_quality=["unsupported"],
                metadata={"file_name": path.name},
            )
        try:
            if path.exists() and path.stat().st_size == 0:
                return parser.empty_result(path, context)
            return parser.parse(path, context)
        except Exception as exc:  # pragma: no cover - last-resort containment
            return ParseResult(
                parser_name=parser.name,
                parser_version=parser.version,
                profile_id=context.profile_id,
                file_family=file_family,
                role=parser.role,
                diagnostics=[
                    ParseDiagnosticRecord(
                        severity="error",
                        code="parser_exception",
                        message=str(exc),
                    )
                ],
                data_quality=["partial_recovery"],
                metadata={"file_name": path.name},
            )

    def families(self) -> list[SourceFileFamily]:
        return sorted(self._parsers.keys(), key=str)

