from pathlib import Path

from domain.enums.common import FileRole, SourceFileFamily
from parsers.base.registry import ParserRegistry
from parsers.formats.burn_log import BurnLogParser
from parsers.formats.error_log import ErrorLogParser
from parsers.formats.event_log import EventLogParser
from parsers.formats.monitor100_log import Monitor100LogParser
from parsers.formats.monitor200_log import Monitor200LogParser
from parsers.formats.sensors_log import SensorsLogParser
from parsers.formats.stateflow_log import StateFlowLogParser
from parsers.formats.stateflowdata import StateFlowDataParser
from parsers.formats.table_temp_log import TableTempLogParser
from parsers.formats.time_log import TimeLogParser
from profiles.base.profile import FileFamilySpec, PrinterProfilePlugin, load_yaml


PROFILE_ID = "laser-systems-m350"
PROFILE_VERSION = "0.2.0"


class M350Profile(PrinterProfilePlugin):
    def __init__(self) -> None:
        config_dir = Path(__file__).resolve().parent
        raw_yaml = load_yaml(config_dir / "signals.yaml")
        signals = raw_yaml.get("signals", {})
        machine_meta = raw_yaml.get("machine", {})
        phases = load_yaml(config_dir / "phases.yaml")
        stateflow = load_yaml(config_dir / "stateflow.yaml")
        super().__init__(
            profile_id=PROFILE_ID,
            vendor=machine_meta.get("vendor", "АО «Лазерные системы»"),
            model_family=machine_meta.get("model", "M-350"),
            legacy_names=["M-450-M", "M-350"],
            version=PROFILE_VERSION,
            serial_number=machine_meta.get("serial_number", ""),
            passport=machine_meta.get("passport", ""),
            config_dir=config_dir,
            file_families=[
                FileFamilySpec("*.log", SourceFileFamily.main_event_log, FileRole.primary),
                FileFamilySpec("*_burn.log", SourceFileFamily.burn_log, FileRole.primary),
                FileFamilySpec("*_time.log", SourceFileFamily.time_log, FileRole.secondary),
                FileFamilySpec("*_sensors.log", SourceFileFamily.sensors_log, FileRole.secondary),
                FileFamilySpec("*_Monitor100.log", SourceFileFamily.monitor100_log, FileRole.primary),
                FileFamilySpec("*_Monitor200.log", SourceFileFamily.monitor200_log, FileRole.auxiliary),
                FileFamilySpec("*_stateFlow.log", SourceFileFamily.stateflow_log, FileRole.primary),
                FileFamilySpec("*_stateFlowData.log", SourceFileFamily.stateflowdata_log, FileRole.auxiliary),
                FileFamilySpec("table_temp.log", SourceFileFamily.table_temp_log, FileRole.secondary),
                FileFamilySpec("*_error.log", SourceFileFamily.error_log, FileRole.auxiliary),
            ],
            signal_mappings=signals,
            phase_rules=phases,
            stateflow_mapping=stateflow,
        )

    def register_parsers(self, registry: ParserRegistry) -> None:
        registry.register(EventLogParser())
        registry.register(BurnLogParser())
        registry.register(TimeLogParser())
        registry.register(SensorsLogParser())
        registry.register(Monitor100LogParser())
        registry.register(Monitor200LogParser())
        registry.register(StateFlowLogParser())
        registry.register(StateFlowDataParser())
        registry.register(TableTempLogParser())
        registry.register(ErrorLogParser())


def get_profile() -> M350Profile:
    return M350Profile()


def build_registry() -> ParserRegistry:
    registry = ParserRegistry()
    get_profile().register_parsers(registry)
    return registry

