from core.versioning.provenance import build_provenance, stable_hash


def test_stable_hash_is_order_independent_for_mappings():
    assert stable_hash({"a": 1, "b": 2}) == stable_hash({"b": 2, "a": 1})


def test_provenance_carries_reproducible_input_and_config_hashes():
    first = build_provenance(
        "test",
        inputs={"file": "abc"},
        config={"threshold": 3.5},
        parser_versions={"time": "1.0"},
    )
    second = build_provenance(
        "test",
        inputs={"file": "abc"},
        config={"threshold": 3.5},
        parser_versions={"time": "1.0"},
    )
    assert first["input_fingerprint"] == second["input_fingerprint"]
    assert first["config_hash"] == second["config_hash"]
    assert first["app_version"]
    assert first["analysis_version"]
    assert first["parser_versions"] == {"time": "1.0"}
