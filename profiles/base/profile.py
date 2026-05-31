from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from domain.enums.common import FileRole, SourceFileFamily
from parsers.base.registry import ParserRegistry


@dataclass(frozen=True)
class FileFamilySpec:
    pattern: str
    family: SourceFileFamily
    role: FileRole
    notes: str = ""


@dataclass
class PrinterProfilePlugin:
    profile_id: str
    vendor: str
    model_family: str
    legacy_names: list[str]
    version: str
    file_families: list[FileFamilySpec]
    config_dir: Path
    signal_mappings: dict[str, dict[str, Any]] = field(default_factory=dict)
    phase_rules: dict[str, Any] = field(default_factory=dict)
    stateflow_mapping: dict[str, Any] = field(default_factory=dict)
    # Optional machine identity — populated from signals.yaml `machine:` block when present.
    serial_number: str = ""
    passport: str = ""

    def register_parsers(self, registry: ParserRegistry) -> None:
        raise NotImplementedError


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}

