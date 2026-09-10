from analytics.prediction.pair_matching import score_print_session_pair


def test_exact_layers_and_near_date_are_strong() -> None:
    result = score_print_session_pair(
        date_delta_hours=20,
        expected_layers=950,
        observed_last_layer=950,
        expected_material="steel",
    )
    assert result.confidence == "strong"
    assert result.auto_link_allowed is True
    assert result.layer_error_pct == 0


def test_date_alone_never_becomes_strong() -> None:
    result = score_print_session_pair(
        date_delta_hours=0,
        expected_layers=None,
        observed_last_layer=950,
    )
    assert result.confidence == "weak"
    assert result.auto_link_allowed is False


def test_gross_layer_mismatch_rejects_same_day_pair() -> None:
    result = score_print_session_pair(
        date_delta_hours=1,
        expected_layers=423,
        observed_last_layer=1983,
    )
    assert result.eligible is False
    assert result.confidence == "rejected"


def test_material_contradiction_rejects_pair() -> None:
    result = score_print_session_pair(
        date_delta_hours=1,
        expected_layers=100,
        observed_last_layer=100,
        expected_material="Алюминий",
        observed_material="steel",
    )
    assert result.eligible is False


def test_explicit_import_still_needs_no_hard_contradiction() -> None:
    plausible = score_print_session_pair(
        date_delta_hours=2,
        expected_layers=None,
        observed_last_layer=None,
        explicit_import_hint=True,
    )
    contradicted = score_print_session_pair(
        date_delta_hours=2,
        expected_layers=100,
        observed_last_layer=500,
        explicit_import_hint=True,
    )
    assert plausible.eligible is True
    assert contradicted.eligible is False
