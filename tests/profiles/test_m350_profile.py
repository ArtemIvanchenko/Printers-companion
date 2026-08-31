from domain.enums.common import SourceFileFamily
from profiles.m350.profile import build_registry, get_profile
from profiles.signal_catalog import signal_display_name, signal_metadata


def test_m350_profile_loads_with_legacy_name_and_parsers() -> None:
    profile = get_profile()
    registry = build_registry()

    assert profile.model_family == "M-450M"
    assert "M-350" in profile.legacy_names
    assert SourceFileFamily.stateflow_log in registry.families()
    assert "SO1" in profile.signal_mappings


def test_m350_profile_passport_metadata() -> None:
    profile = get_profile()
    assert profile.serial_number == "M003-005"
    assert profile.passport == "САЦН.681749.002ПС"
    assert profile.vendor == "АО «Лазерные системы»"


def test_m350_signal_alarm_thresholds() -> None:
    profile = get_profile()
    so1 = profile.signal_mappings.get("SO1", {})
    assert so1.get("alarm_high") == 2.0
    assert so1.get("max_val") == 20.0

    st5 = profile.signal_mappings.get("ST5", {})
    assert st5.get("alarm_high") == 200

    sf1 = profile.signal_mappings.get("SF1", {})
    assert sf1.get("unit") == "raw_sensor_units"
    assert "min_val" not in sf1

    flow_t = profile.signal_mappings.get("Flow T", {})
    assert flow_t.get("invalid_values") == [125.0]

    lir = profile.signal_mappings.get("LIR", {})
    assert lir.get("min_val") == -420000


def test_signal_catalog_exposes_readable_russian_names_without_renaming_raw_keys() -> None:
    profile = get_profile()
    assert "Flow T" in profile.signal_mappings
    assert signal_display_name("Flow T") == "Температура продувочного газа"
    assert signal_display_name("SO1") == "Кислород в рабочей камере — датчик 1"
    assert signal_metadata("SP4")["unit_display_ru"] == "бар"
    assert signal_metadata("SP99")["display_name_ru"] == "Канал давления SP99"
