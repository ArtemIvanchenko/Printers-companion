from pathlib import Path

from domain.enums.common import FileRole, SourceFileFamily
from domain.schemas.parsing import FileClassification


PATTERNS: list[tuple[str, SourceFileFamily, FileRole]] = [
    ("*_Monitor100.log", SourceFileFamily.monitor100_log, FileRole.primary),
    ("*_Monitor200.log", SourceFileFamily.monitor200_log, FileRole.auxiliary),
    ("*_stateFlow.log", SourceFileFamily.stateflow_log, FileRole.primary),
    ("*_stateFlowData.log", SourceFileFamily.stateflowdata_log, FileRole.auxiliary),
    ("*_burn.log", SourceFileFamily.burn_log, FileRole.primary),
    ("*_time.log", SourceFileFamily.time_log, FileRole.secondary),
    ("*_sensors.log", SourceFileFamily.sensors_log, FileRole.secondary),
    ("*_error.log", SourceFileFamily.error_log, FileRole.auxiliary),
    ("table_temp.log", SourceFileFamily.table_temp_log, FileRole.secondary),
    ("*table*temp*.log", SourceFileFamily.table_temp_log, FileRole.secondary),
    ("*.log", SourceFileFamily.main_event_log, FileRole.primary),
]


def classify_file(path: Path) -> FileClassification:
    for pattern, family, role in PATTERNS:
        if path.match(pattern):
            confidence = 0.95 if pattern != "*.log" else 0.55
            return FileClassification(
                path=str(path),
                file_name=path.name,
                family=family,
                role=role,
                confidence=confidence,
                matched_pattern=pattern,
            )
    return FileClassification(
        path=str(path),
        file_name=path.name,
        family=SourceFileFamily.unsupported,
        role=FileRole.unknown,
        confidence=0.0,
    )

