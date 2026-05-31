from pathlib import Path

from domain.enums.common import FileRole, SourceFileFamily
from domain.schemas.parsing import ParseDiagnosticRecord, ParseResult
from parsers.base.base import BaseParser, ParserContext
from parsers.common.encoding import estimate_encoding, is_probably_binary


class StateFlowDataParser(BaseParser):
    name = "stateflowdata_log"
    version = "0.1.0"
    file_family = SourceFileFamily.stateflowdata_log
    role = FileRole.auxiliary

    def parse(self, path: Path, context: ParserContext) -> ParseResult:
        if path.stat().st_size == 0:
            return self.empty_result(path, context)
        if is_probably_binary(path):
            return ParseResult(
                parser_name=self.name,
                parser_version=self.version,
                profile_id=context.profile_id,
                file_family=self.file_family,
                role=self.role,
                diagnostics=[
                    ParseDiagnosticRecord(
                        severity="warning",
                        code="unsupported_stateflow_data",
                        message="stateFlowData appears binary or unsupported; metadata preserved.",
                    )
                ],
                data_quality=["binary_or_unknown", "unsupported"],
                metadata={"size_bytes": path.stat().st_size, "format": "binary_or_unknown"},
            )
        encoding = estimate_encoding(path)
        return ParseResult(
            parser_name=self.name,
            parser_version=self.version,
            profile_id=context.profile_id,
            file_family=self.file_family,
            role=self.role,
            diagnostics=[
                ParseDiagnosticRecord(
                    severity="info",
                    code="stateflow_data_text_preserved",
                    message="stateFlowData is text-like but has no specialized parser yet.",
                )
            ],
            data_quality=["unsupported"],
            metadata={"size_bytes": path.stat().st_size, "format": "text", "encoding": encoding},
        )

