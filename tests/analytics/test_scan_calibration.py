"""Scan-time calibration (Level B): fitting real burn_ms against stored geometry.

No PySLM needed here: the geometry series is handcrafted — the fit consumes the
snapshot form, exactly as production does (geometry is persisted at estimate
time; calibration never re-slices).
"""
from datetime import datetime, timezone

import pytest

pytest.importorskip("scipy")

from analytics.prediction.layer_engine import (  # noqa: E402
    GEOMETRY_FEATURES,
    LayerGeometrySeries,
    resolve_scan_model,
    scan_model_key,
    scan_seconds_from_model,
)
from analytics.prediction.scan_calibration import (  # noqa: E402
    MIN_FIT_R2,
    MIN_LAYERS_FOR_FIT,
    MIN_PRINTS_FOR_FIT,
    _burn_seconds_by_layer,
    recalibrate_scan_and_apply,
    scan_calibration_report,
)
from domain.enums.common import SourceFileFamily  # noqa: E402
from domain.models.prints import MachineParams, PrintRecord  # noqa: E402
from domain.models.sessions import BuildSession  # noqa: E402
from domain.schemas.parsing import FileClassification  # noqa: E402
from domain.services.ingestion import IngestedFile  # noqa: E402
from storage.db.session import SessionLocal  # noqa: E402
from storage.repositories.runtime import RuntimeRepository  # noqa: E402

THICKNESS = 0.1
LASERS = 2
N_LAYERS = 200
# "True" per-layer model used to synthesise burn times: seconds =
# hatch/1000 + contour/430 + jump/3000 + n_jumps*0.01 (per laser) + 0.5 фикс.
TRUE_BETA = [1 / 1000, 1 / 430, 1 / 3000, 0.01, 0.0]
TRUE_INTERCEPT = 0.5


def _series() -> LayerGeometrySeries:
    """Varying geometry: hatch ramps 2000→500 mm so the fit is identifiable."""
    zs = [(i + 0.5) * THICKNESS for i in range(N_LAYERS)]
    hatch = [2000.0 - 1500.0 * i / (N_LAYERS - 1) for i in range(N_LAYERS)]
    return LayerGeometrySeries(
        zs=zs,
        hatch_mm=hatch,
        contour_mm=[100.0] * N_LAYERS,
        jump_mm=[h * 1.5 for h in hatch],
        n_jumps=[h / 10.0 for h in hatch],
        open_mm=[0.0] * N_LAYERS,
        z_min=0.0,
        z_max=N_LAYERS * THICKNESS,
    )


def _burn_seconds(series: LayerGeometrySeries, layer: int) -> float:
    z = series.z_min + (layer - 0.5) * THICKNESS
    g = series.at(z)
    return sum(b * v for b, v in zip(TRUE_BETA, g)) / LASERS + TRUE_INTERCEPT


def _snapshot_geometry(series: LayerGeometrySeries) -> dict:
    return {**series.to_snapshot(), "layer_thickness_mm": THICKNESS, "laser_count": LASERS}


@pytest.fixture
def db():
    with SessionLocal() as session:
        yield session
        session.rollback()


def _write_time_log(tmp_path, session_id: str, burn_ms_by_layer: dict) -> str:
    lines = [
        f"OLD_STATS: {layer} | 9250 | {int(burn)} | {int(burn) + 9250} |"
        for layer, burn in sorted(burn_ms_by_layer.items())
    ]
    path = tmp_path / f"{session_id}_time.log"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def _linked_pair(db, tmp_path, record_id: str, series: LayerGeometrySeries,
                 burn_ms_by_layer: dict, material: str = "steel",
                 classification: str = "REAL_PRINT") -> None:
    session_id = f"s_{record_id}"
    log_path = _write_time_log(tmp_path, session_id, burn_ms_by_layer)
    ingested = IngestedFile(
        path=log_path, relative_path=f"{session_id}_time.log",
        classification=FileClassification(
            path=log_path, file_name=f"{session_id}_time.log",
            family=SourceFileFamily.time_log, role="secondary", confidence=1.0,
        ),
        checksum="x", size_bytes=1, data_quality_status="ok",
        mtime=datetime.now(timezone.utc), parse_result=None,
    )
    RuntimeRepository(db).save_session_payload(
        session_id,
        {"files": [ingested.model_dump(mode="json")], "group": {"classification": classification}},
    )
    row = db.get(BuildSession, session_id)
    row.classification = classification
    row.start_ts = datetime(2027, 3, 1, 8, tzinfo=timezone.utc)
    db.add(PrintRecord(
        record_id=record_id, name=record_id, material=material, session_id=session_id,
        metadata_json={"prediction": {
            "material": material,
            "scan_geometry": _snapshot_geometry(series),
        }},
    ))


class TestBurnExtraction:
    def test_extracts_and_guards(self):
        events = [
            {"event_type": "layer_timing_summary", "payload": {"layer": 1, "burn_ms": 40000}},
            {"event_type": "layer_timing_summary", "payload": {"layer": 1, "burn_ms": 999}},  # dup
            {"event_type": "layer_timing_summary", "payload": {"layer": 2, "burn_ms": 5}},    # implausible
            {"event_type": "pour_start", "payload": {"layer": 3, "abs_ms": 1}},
        ]
        assert _burn_seconds_by_layer(events) == {1: 40.0}


class TestFitAndApply:
    def test_recovers_prediction_and_applies(self, db, tmp_path):
        series = _series()
        burn = {L: _burn_seconds(series, L) * 1000 for L in range(1, N_LAYERS + 1)}
        db.add(MachineParams(id=1, hatch_speed_mm_s=1000, laser_count=LASERS))
        _linked_pair(db, tmp_path, "pr_fit_a", series, burn)
        _linked_pair(db, tmp_path, "pr_fit_b", series, burn)
        db.flush()

        result = recalibrate_scan_and_apply(db)
        key = scan_model_key("steel", THICKNESS)
        assert key in result["applied"], result
        model = result["applied"][key]
        assert model["r2"] > 0.99
        assert model["n_prints"] == 2
        assert model["cv_worst_abs_total_err_pct"] < 1.0
        assert abs(model["total_err_pct"]) < 1.0
        assert model["features"] == list(GEOMETRY_FEATURES)

        # Stored and resolvable exactly like the estimator does it
        params = {"scan_model_by_mat": db.get(MachineParams, 1).scan_model_by_mat}
        resolved = resolve_scan_model(params, "steel", THICKNESS)
        assert resolved is not None
        # Prediction from the fitted model reproduces the synthetic total
        totals = series.totals(THICKNESS)
        pred_s = scan_seconds_from_model(totals, N_LAYERS, LASERS, resolved)
        real_s = sum(burn.values()) / 1000
        assert pred_s == pytest.approx(real_s, rel=0.02)

    def test_model_never_applies_to_another_mode(self, db, tmp_path):
        series = _series()
        burn = {L: _burn_seconds(series, L) * 1000 for L in range(1, N_LAYERS + 1)}
        db.add(MachineParams(id=1))
        _linked_pair(db, tmp_path, "pr_mode_a", series, burn)
        _linked_pair(db, tmp_path, "pr_mode_b", series, burn)
        db.flush()
        recalibrate_scan_and_apply(db)

        params = {"scan_model_by_mat": db.get(MachineParams, 1).scan_model_by_mat}
        assert resolve_scan_model(params, "steel", THICKNESS) is not None
        # Real-data validation showed cross-mode transfer is worse than useless
        # (R² < 0) — a different thickness or material must resolve to nothing.
        assert resolve_scan_model(params, "steel", 0.025) is None
        assert resolve_scan_model(params, "aluminum", THICKNESS) is None

    def test_noise_only_data_is_rejected_by_r2_gate(self, db, tmp_path):
        import random

        rng = random.Random(0)
        series = _series()
        burn_a = {L: rng.uniform(5_000, 50_000) for L in range(1, N_LAYERS + 1)}
        burn_b = {L: rng.uniform(5_000, 50_000) for L in range(1, N_LAYERS + 1)}
        db.add(MachineParams(id=1))
        _linked_pair(db, tmp_path, "pr_noise_a", series, burn_a)
        _linked_pair(db, tmp_path, "pr_noise_b", series, burn_b)
        db.flush()

        result = recalibrate_scan_and_apply(db)
        assert result["applied"] == {}
        assert result["skipped"]
        assert MIN_FIT_R2 > 0

    def test_single_print_is_rejected_even_with_perfect_in_sample_fit(self, db, tmp_path):
        series = _series()
        burn = {layer: _burn_seconds(series, layer) * 1000
                for layer in range(1, N_LAYERS + 1)}
        db.add(MachineParams(id=1))
        _linked_pair(db, tmp_path, "pr_single", series, burn)
        db.flush()

        result = recalibrate_scan_and_apply(db)
        assert result["applied"] == {}
        assert any("too_few_prints" in item["reason"] for item in result["skipped"])
        assert MIN_PRINTS_FOR_FIT == 2

    def test_too_few_layers_rejected(self, db, tmp_path):
        series = _series()
        per_print_layers = MIN_LAYERS_FOR_FIT // 2 - 5
        burn = {L: _burn_seconds(series, L) * 1000 for L in range(1, per_print_layers + 1)}
        db.add(MachineParams(id=1))
        _linked_pair(db, tmp_path, "pr_few_a", series, burn)
        _linked_pair(db, tmp_path, "pr_few_b", series, burn)
        db.flush()

        result = recalibrate_scan_and_apply(db)
        assert result["applied"] == {}
        assert any("too_few_layers" in s["reason"] for s in result["skipped"])

    def test_locked_params_block_apply(self, db, tmp_path):
        series = _series()
        burn = {L: _burn_seconds(series, L) * 1000 for L in range(1, N_LAYERS + 1)}
        db.add(MachineParams(id=1, correction_locked=True))
        _linked_pair(db, tmp_path, "pr_lock_a", series, burn)
        _linked_pair(db, tmp_path, "pr_lock_b", series, burn)
        db.flush()

        result = recalibrate_scan_and_apply(db)
        assert result["locked"] is True
        assert result["applied"] == {}

    def test_non_print_sessions_are_excluded(self, db, tmp_path):
        series = _series()
        burn = {L: _burn_seconds(series, L) * 1000 for L in range(1, N_LAYERS + 1)}
        db.add(MachineParams(id=1))
        _linked_pair(db, tmp_path, "pr_svc", series, burn, classification="SERVICE_SESSION")
        db.flush()

        report = scan_calibration_report(db)
        assert report["candidates"] == {}
        assert any(r.get("reason") == "not_a_print" for r in report["records"])

    def test_duplicate_session_links_are_excluded(self, db, tmp_path):
        series = _series()
        burn = {layer: _burn_seconds(series, layer) * 1000
                for layer in range(1, N_LAYERS + 1)}
        db.add(MachineParams(id=1))
        _linked_pair(db, tmp_path, "pr_dup_a", series, burn)
        db.add(PrintRecord(
            record_id="pr_dup_b", name="pr_dup_b", material="steel",
            session_id="s_pr_dup_a",
            metadata_json={"prediction": {
                "material": "steel", "scan_geometry": _snapshot_geometry(series),
            }},
        ))
        db.flush()

        report = scan_calibration_report(db)
        assert report["candidates"] == {}
        assert all(row["reason"] == "duplicate_session_link" for row in report["records"])

    def test_partial_layer_coverage_still_fits(self, db, tmp_path):
        """The multi-day log-splitting bug leaves a session with only part of a
        print's layers — that narrows the sample but must not break the fit."""
        series = _series()
        burn_a = {L: _burn_seconds(series, L) * 1000 for L in range(1, 181)}
        burn_b = {L: _burn_seconds(series, L) * 1000 for L in range(21, 201)}
        db.add(MachineParams(id=1))
        _linked_pair(db, tmp_path, "pr_part_a", series, burn_a)
        _linked_pair(db, tmp_path, "pr_part_b", series, burn_b)
        db.flush()

        result = recalibrate_scan_and_apply(db)
        assert scan_model_key("steel", THICKNESS) in result["applied"]

    def test_stale_models_without_candidates_are_removed(self, db):
        db.add(MachineParams(
            id=1,
            scan_model_by_mat={
                "steel@0.100": {"beta": [1.0]},
                "aluminum@0.060": {"beta": [2.0]},
            },
        ))
        db.flush()

        result = recalibrate_scan_and_apply(db)
        assert set(result["removed"]) == {"steel@0.100", "aluminum@0.060"}
        assert db.get(MachineParams, 1).scan_model_by_mat == {}
