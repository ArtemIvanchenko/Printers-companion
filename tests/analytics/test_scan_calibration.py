"""Scan-time calibration (Level B): fitting real burn_ms against stored geometry.

No PySLM needed here: the geometry series is handcrafted — the fit consumes the
snapshot form, exactly as production does (geometry is persisted at estimate
time; calibration never re-slices).
"""
from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError

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
    _fit_layer_cycle_model,
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


def _series(scale: float = 1.0) -> LayerGeometrySeries:
    """Varying geometry: hatch ramps 2000→500 mm so the fit is identifiable."""
    zs = [(i + 0.5) * THICKNESS for i in range(N_LAYERS)]
    hatch = [
        (2000.0 - 1500.0 * i / (N_LAYERS - 1)) * scale
        for i in range(N_LAYERS)
    ]
    return LayerGeometrySeries(
        zs=zs,
        hatch_mm=hatch,
        contour_mm=[100.0 * scale] * N_LAYERS,
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


def _write_time_log(
    tmp_path, session_id: str, burn_ms_by_layer: dict,
    make_ms_by_layer: dict[int, float] | None = None,
) -> str:
    lines = [
        f"OLD_STATS: {layer} | 9250 | {int(burn)} | "
        f"{int((make_ms_by_layer or {}).get(layer, int(burn) + 9500))} |"
        for layer, burn in sorted(burn_ms_by_layer.items())
    ]
    path = tmp_path / f"{session_id}_time.log"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def _linked_pair(db, tmp_path, record_id: str, series: LayerGeometrySeries,
                 burn_ms_by_layer: dict, material: str = "steel",
                 classification: str = "REAL_PRINT",
                 geometry_fingerprint: str | None = None,
                 make_ms_by_layer: dict[int, float] | None = None) -> None:
    session_id = f"s_{record_id}"
    log_path = _write_time_log(
        tmp_path, session_id, burn_ms_by_layer, make_ms_by_layer,
    )
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
    prediction = {
        "material": material,
        "scan_geometry": _snapshot_geometry(series),
    }
    if geometry_fingerprint is not None:
        prediction["geometry_fingerprint"] = geometry_fingerprint
    db.add(PrintRecord(
        record_id=record_id, name=record_id, material=material, session_id=session_id,
        metadata_json={"prediction": prediction},
    ))


class TestBurnExtraction:
    def test_extracts_and_guards(self):
        events = [
            {"event_type": "layer_timing_summary", "payload": {"layer": 1, "burn_ms": 40000}},
            {"event_type": "layer_timing_summary", "payload": {"layer": 1, "burn_ms": 40010}},  # equivalent dup
            {"event_type": "layer_timing_summary", "payload": {"layer": 2, "burn_ms": 5}},    # implausible
            {"event_type": "layer_timing_summary", "payload": {"layer": 3, "burn_ms": 40000}},
            {"event_type": "layer_timing_summary", "payload": {"layer": 3, "burn_ms": 20000}},  # retry
            {"event_type": "pour_start", "payload": {"layer": 3, "abs_ms": 1}},
        ]
        assert _burn_seconds_by_layer(events) == {1: 40.0}


class TestLayerCycleFit:
    def test_recovers_base_and_minimum_cycle_robustly(self):
        components_a = [10_000.0 + index * 100.0 for index in range(180)]
        components_b = [10_100.0 + index * 105.0 for index in range(180)]
        make_a = [max(value + 400.0, 20_000.0) for value in components_a]
        make_b = [max(value + 400.0, 20_000.0) for value in components_b]
        make_b[70] += 20_000.0  # restart-like outlier; soft-L1 must not move the kink

        model = _fit_layer_cycle_model(
            [(components_a, make_a), (components_b, make_b)],
            ["geometry-a", "geometry-b"],
        )

        assert model is not None
        assert model["minimum_cycle_status"] == "identified"
        assert model["base_overhead_ms"] == pytest.approx(400.0, abs=30.0)
        assert model["minimum_cycle_ms"] == pytest.approx(20_000.0, abs=80.0)
        assert model["floor_n_prints"] == 2
        assert model["free_n_prints"] == 2
        assert model["floor_robust_loss_improvement_pct"] >= 5.0

    def test_floor_seen_in_only_one_print_is_not_published(self):
        low_and_high = [10_000.0 + index * 100.0 for index in range(180)]
        high_only = [22_000.0 + index * 100.0 for index in range(180)]
        model = _fit_layer_cycle_model(
            [
                (low_and_high, [max(value + 400.0, 20_000.0) for value in low_and_high]),
                (high_only, [value + 400.0 for value in high_only]),
            ],
            ["geometry-a", "geometry-b"],
        )

        assert model is not None
        assert model["minimum_cycle_ms"] is None
        assert model["minimum_cycle_status"] == "unidentified"


class TestFitAndApply:
    def test_recovers_prediction_and_applies(self, db, tmp_path):
        series = _series()
        other_series = _series(0.8)
        burn = {L: _burn_seconds(series, L) * 1000 for L in range(1, N_LAYERS + 1)}
        other_burn = {
            L: _burn_seconds(other_series, L) * 1000
            for L in range(1, N_LAYERS + 1)
        }
        db.add(MachineParams(id=1, hatch_speed_mm_s=1000, laser_count=LASERS))
        _linked_pair(db, tmp_path, "pr_fit_a", series, burn)
        _linked_pair(db, tmp_path, "pr_fit_b", other_series, other_burn)
        db.flush()

        result = recalibrate_scan_and_apply(db)
        key = scan_model_key("steel", THICKNESS)
        assert key in result["applied"], result
        model = result["applied"][key]
        assert model["r2"] > 0.99
        assert model["n_prints"] == 2
        assert model["cv_worst_abs_total_err_pct"] < 10.0
        assert abs(model["total_err_pct"]) < 1.0
        assert model["features"] == list(GEOMETRY_FEATURES)
        cycle_model = result["cycle_applied"][key]
        assert cycle_model["base_overhead_ms"] == pytest.approx(250.0)
        assert cycle_model["n_prints"] == 2
        assert cycle_model["minimum_cycle_ms"] is None

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
        other_series = _series(0.8)
        burn = {L: _burn_seconds(series, L) * 1000 for L in range(1, N_LAYERS + 1)}
        other_burn = {
            L: _burn_seconds(other_series, L) * 1000
            for L in range(1, N_LAYERS + 1)
        }
        db.add(MachineParams(id=1))
        _linked_pair(db, tmp_path, "pr_mode_a", series, burn)
        _linked_pair(db, tmp_path, "pr_mode_b", other_series, other_burn)
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
        other_series = _series(0.8)
        burn_a = {L: rng.uniform(5_000, 50_000) for L in range(1, N_LAYERS + 1)}
        burn_b = {L: rng.uniform(5_000, 50_000) for L in range(1, N_LAYERS + 1)}
        db.add(MachineParams(id=1))
        _linked_pair(db, tmp_path, "pr_noise_a", series, burn_a)
        _linked_pair(db, tmp_path, "pr_noise_b", other_series, burn_b)
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

    def test_exact_reprints_share_one_cv_fold_and_do_not_satisfy_geometry_gate(
        self, db, tmp_path,
    ):
        series = _series()
        burn = {
            layer: _burn_seconds(series, layer) * 1000
            for layer in range(1, N_LAYERS + 1)
        }
        db.add(MachineParams(id=1))
        _linked_pair(
            db, tmp_path, "pr_twin_a", series, burn,
            geometry_fingerprint="files-sha256:same-layout",
        )
        _linked_pair(
            db, tmp_path, "pr_twin_b", series, burn,
            geometry_fingerprint="files-sha256:same-layout",
        )
        db.flush()

        result = recalibrate_scan_and_apply(db)

        assert result["applied"] == {}
        assert any(
            "too_few_unique_geometries" in item["reason"]
            for item in result["skipped"]
        )

    def test_cycle_fit_is_independent_of_scan_geometry_and_scoped_by_machine(
        self, db, tmp_path,
    ):
        series = _series()
        burn = {
            layer: _burn_seconds(series, layer) * 1000
            for layer in range(1, N_LAYERS + 1)
        }
        db.add(MachineParams(id=1))
        for machine, overhead in (("m350-a", 250.0), ("m350-b", 700.0)):
            for suffix in ("one", "two"):
                record_id = f"pr_{machine}_{suffix}"
                makes = {
                    layer: value + 9_250.0 + overhead
                    for layer, value in burn.items()
                }
                _linked_pair(
                    db, tmp_path, record_id, series, burn,
                    geometry_fingerprint=f"files-sha256:{machine}:{suffix}",
                    make_ms_by_layer=makes,
                )
                db.flush()
                row = db.get(PrintRecord, record_id)
                prediction = dict(row.metadata_json["prediction"])
                prediction.pop("scan_geometry")
                prediction.update({
                    "layer_thickness_mm": THICKNESS,
                    "laser_count": LASERS,
                    "printer_id": machine,
                })
                row.metadata_json = {
                    "calibration_exclusions": ["scan", "time"],
                    "prediction": prediction,
                }
        db.flush()

        result = recalibrate_scan_and_apply(db)

        assert result["applied"] == {}
        key_a = f"m350-a|steel@{THICKNESS:.3f}|lasers={LASERS}"
        key_b = f"m350-b|steel@{THICKNESS:.3f}|lasers={LASERS}"
        assert set(result["cycle_applied"]) == {key_a, key_b}
        assert result["cycle_applied"][key_a]["base_overhead_ms"] == pytest.approx(
            250.0, abs=2.0,
        )
        assert result["cycle_applied"][key_b]["base_overhead_ms"] == pytest.approx(
            700.0, abs=2.0,
        )

    def test_too_few_layers_rejected(self, db, tmp_path):
        series = _series()
        other_series = _series(0.8)
        per_print_layers = MIN_LAYERS_FOR_FIT // 2 - 5
        burn = {L: _burn_seconds(series, L) * 1000 for L in range(1, per_print_layers + 1)}
        other_burn = {
            L: _burn_seconds(other_series, L) * 1000
            for L in range(1, per_print_layers + 1)
        }
        db.add(MachineParams(id=1))
        _linked_pair(db, tmp_path, "pr_few_a", series, burn)
        _linked_pair(db, tmp_path, "pr_few_b", other_series, other_burn)
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

    def test_database_rejects_duplicate_session_links(self, db, tmp_path):
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
        with pytest.raises(IntegrityError, match="print_records.session_id"):
            db.flush()

    def test_partial_layer_coverage_still_fits(self, db, tmp_path):
        """The multi-day log-splitting bug leaves a session with only part of a
        print's layers — that narrows the sample but must not break the fit."""
        series = _series()
        other_series = _series(0.8)
        burn_a = {L: _burn_seconds(series, L) * 1000 for L in range(1, 181)}
        burn_b = {
            L: _burn_seconds(other_series, L) * 1000 for L in range(21, 201)
        }
        db.add(MachineParams(id=1))
        _linked_pair(db, tmp_path, "pr_part_a", series, burn_a)
        _linked_pair(db, tmp_path, "pr_part_b", other_series, burn_b)
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
