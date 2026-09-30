"""The prints list must show plan and outcome side by side.

The prediction lives in the record's own snapshot, the outcome lives on the
linked log session, and until now nothing joined them — so "did the estimate
hold?" could not be answered from the list at all.
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from api.main import app
from domain.models.events import LayerSnapshot
from domain.models.prints import PrintRecord
from domain.models.sessions import BuildSession
from core.versioning.provenance import stable_hash
from storage.db.session import SessionLocal

client = TestClient(app)


@pytest.fixture
def db():
    with SessionLocal() as session:
        yield session
        session.rollback()


def _session(db, session_id: str, *, machine_min=None, duration_min=None,
             idle_min=None, explicit_pause_seconds=None, layers=174, classification="REAL_PRINT"):
    start = datetime(2026, 3, 27, 10, tzinfo=timezone.utc)
    db.add(BuildSession(
        session_id=session_id,
        start_ts=start,
        end_ts=start + timedelta(minutes=duration_min) if duration_min else None,
        classification=classification,
        context={"runtime_payload": {"group": {
            "classification": classification,
            "features": {
                "machine_min": machine_min, "duration_min": duration_min,
                "idle_min": idle_min, "layers": layers,
                "explicit_pause_seconds": explicit_pause_seconds,
            },
        }}},
    ))
    if machine_min and layers:
        pour_ms = 500.0
        burn_ms = machine_min * 60_000 / layers - pour_ms
        db.add_all([
            LayerSnapshot(
                session_id=session_id, layer=layer,
                features={"burn_ms": burn_ms, "pour_ms": pour_ms},
            )
            for layer in range(1, layers + 1)
        ])
    db.commit()


def _record(db, record_id: str, *, session_id=None, predicted_hours=None, cost=None,
            layer_count=174, machine_cycle_hours=None):
    metadata = {}
    if predicted_hours is not None:
        metadata["prediction"] = {
            "print_hours": predicted_hours,
            "cost_total_rub": cost,
            "layer_count": layer_count,
        }
        if machine_cycle_hours is not None:
            metadata["prediction"]["machine_cycle_hours"] = machine_cycle_hours
    db.add(PrintRecord(
        record_id=record_id, name=f"печать {record_id}", material="alsi10mg",
        session_id=session_id, metadata_json=metadata,
    ))
    db.commit()


def _summary(record_id: str) -> dict:
    items = client.get("/prints").json()["items"]
    return next(r["summary"] for r in items if r["record_id"] == record_id)


class TestPlanVsFactSummary:
    def test_machine_time_is_the_actual_not_the_wall_span(self, db):
        """The estimate models machine time, so the comparison must use it.

        The legacy quote is scan+recoat, not a full machine-cycle quote.
        Wall-clock minus scan+recoat does not prove operator pauses.
        """
        _session(db, "s_mt", machine_min=264.0, duration_min=492.0, idle_min=228.0)
        _record(db, "pr_mt", session_id="s_mt", predicted_hours=4.1)

        summary = _summary("pr_mt")
        assert summary["actual_hours"] == pytest.approx(4.4)
        assert summary["actual_source"] == "subtotal_machine_log"
        assert summary["comparison_scope"] == "burn_plus_pour"
        assert summary["error_pct"] == pytest.approx(-6.8, abs=0.1)
        assert summary["idle_hours"] is None
        assert summary["diagnostics"]["wall_clock_hours"] == pytest.approx(8.2)

    def test_wall_span_is_never_substituted_for_missing_machine_time(self, db):
        _session(db, "s_ws", duration_min=264.0)
        _record(db, "pr_ws", session_id="s_ws", predicted_hours=4.1)

        summary = _summary("pr_ws")
        assert summary["actual_source"] is None
        assert summary["actual_hours"] is None
        assert summary["error_pct"] is None
        assert summary["diagnostics"]["wall_clock_hours"] == pytest.approx(4.4)

    def test_partial_machine_summary_is_not_trusted(self, db):
        _session(db, "s_partial", machine_min=264.0, duration_min=492.0, layers=174)
        _record(db, "pr_partial", session_id="s_partial", predicted_hours=4.1,
                layer_count=1000)

        summary = _summary("pr_partial")
        assert summary["actual_source"] is None
        assert summary["actual_hours"] is None
        assert summary["error_pct"] is None
        assert summary["comparison_status"] == "partial_coverage"
        assert summary["diagnostics"]["measured_burn_plus_pour_hours"] == pytest.approx(4.4)

    def test_unlinked_record_reports_plan_without_fact(self, db):
        _record(db, "pr_plan", predicted_hours=4.1, cost=12400)

        summary = _summary("pr_plan")
        assert summary["predicted_hours"] == pytest.approx(4.1)
        assert summary["predicted_cost_rub"] == 12400
        assert summary["actual_hours"] is None
        assert summary["error_pct"] is None

    def test_without_estimate_denominator_only_measured_subtotal_is_diagnostic(self, db):
        _session(db, "s_only", machine_min=264.0, layers=174)
        _record(db, "pr_only", session_id="s_only")

        summary = _summary("pr_only")
        assert summary["predicted_hours"] is None
        assert summary["actual_hours"] is None
        assert summary["diagnostics"]["measured_burn_plus_pour_hours"] == pytest.approx(4.4)
        assert summary["layers"] == 174
        assert summary["error_pct"] is None

    def test_explicit_pauses_are_not_legacy_idle(self, db):
        _session(db, "s_pause", machine_min=264, duration_min=492, idle_min=228,
                 explicit_pause_seconds=1800)
        _record(db, "pr_pause", session_id="s_pause", predicted_hours=4.1)
        assert _summary("pr_pause")["idle_hours"] == .5

    @pytest.mark.parametrize("expected", [None, 175, 180])
    def test_95_percent_or_unknown_layer_count_cannot_claim_full_fact(self, db, expected):
        _session(db, "s_coverage", machine_min=264, layers=174)
        _record(db, "pr_coverage", session_id="s_coverage", predicted_hours=4.1,
                layer_count=expected)
        summary = _summary("pr_coverage")
        assert not summary["coverage"]["complete"]
        assert summary["actual_hours"] is None and summary["error_pct"] is None

    def test_new_cycle_quote_never_compares_with_legacy_subtotal(self, db):
        _session(db, "s_new", machine_min=264)
        _record(db, "pr_new", session_id="s_new", predicted_hours=4.1, machine_cycle_hours=5.0)
        summary = _summary("pr_new")
        assert summary["comparison_scope"] == "machine_cycle"
        assert summary["predicted_hours"] == 5.0
        assert summary["actual_hours"] is None and summary["error_pct"] is None

    def test_detail_and_list_return_the_same_summary(self, db):
        _session(db, "s_same", machine_min=264)
        _record(db, "pr_same", session_id="s_same", predicted_hours=4.1)
        assert client.get("/prints/pr_same").json()["summary"] == _summary("pr_same")

    def test_missing_compact_rows_never_parse_raw_logs(self, db, monkeypatch):
        from storage.repositories.runtime import RuntimeRepository
        monkeypatch.setattr(RuntimeRepository, "get_session_files", lambda *a, **k: pytest.fail("raw fallback"))
        _session(db, "s_raw", duration_min=264)
        _record(db, "pr_raw", session_id="s_raw", predicted_hours=4.1)
        assert _summary("pr_raw")["actual_hours"] is None


def _cycle_evidence(db, *, normal_hours=4.42, normal_layers=174, bad_manifest=False):
    """Independent published analysis fixture, not parameters from a quote."""
    from sqlalchemy import select
    sid = "s_cycle"
    _session(db, sid, machine_min=264, duration_min=492)
    rows = db.scalars(select(LayerSnapshot).where(LayerSnapshot.session_id == sid)
                      .order_by(LayerSnapshot.layer)).all()
    for row in rows:
        row.features = {**row.features, "make_layer_ms": row.features["burn_ms"] + row.features["pour_ms"] + 1000}
        row.context = {"publication_id": "cycle-publication"}
    manifest = {"schema": 1, "publication_id": "cycle-publication", "status": "complete",
                "row_count": len(rows), "rows_fingerprint": stable_hash([
                    {"layer": row.layer, "features": row.features} for row in rows])}
    session = db.get(BuildSession, sid)
    group = session.context["runtime_payload"]["group"]
    session.context = {"timing_publication": manifest, "runtime_payload": {"group": {
        **group, "timing_publication_id": "other" if bad_manifest else "cycle-publication",
        "analysis_snapshot": {"schema_version": 1, "analysis_id": "analysis-independent",
            "features": group["features"], "time_accounting": {
                "status": "ok", "source": "calibrated", "normal_time_scope": "eligible_measured_layers_only",
                "normal_unique_layer_seconds": normal_hours * 3600 if normal_hours is not None else None,
                "normal_layer_count": normal_layers, "explicit_pause_seconds": 1800,
            }},
    }}}
    db.commit()
    _record(db, "pr_cycle", session_id=sid, predicted_hours=4.1, machine_cycle_hours=4.5)


class TestPublishedNormalCycle:
    def test_full_cycle_uses_independent_normal_evidence_not_subtotal(self, db):
        _cycle_evidence(db)
        summary = _summary("pr_cycle")
        assert summary["predicted_hours"] == 4.5
        assert summary["actual_hours"] == 4.42
        assert summary["actual_source"] == "normal_machine_log"
        assert summary["comparison_scope"] == "machine_cycle"
        assert summary["error_pct"] == pytest.approx(1.8)
        assert summary["coverage"]["complete"]
        assert summary["idle_hours"] == .5

    @pytest.mark.parametrize("changes", [
        {"normal_hours": None}, {"normal_layers": 173}, {"bad_manifest": True},
        {"normal_hours": 10}, {"normal_hours": 1},
    ])
    def test_missing_partial_inconsistent_normal_evidence_is_not_whole_fact(self, db, changes):
        _cycle_evidence(db, **changes)
        summary = _summary("pr_cycle")
        assert summary["actual_hours"] is None and summary["error_pct"] is None
        assert summary["comparison_status"] == "normal_cycle_unavailable"
        assert summary["diagnostics"]["measured_cycle_hours"] > 4.4

    def test_comparison_math_runs_after_sql_is_released(self, db, monkeypatch):
        from domain.services.print_cards import comparison
        from storage.repositories.prints_repo import PrintsRepository
        _cycle_evidence(db)
        record = PrintsRepository(db).get_print_record("pr_cycle")
        original = comparison.comparison_summary

        def without_sql(*args):
            assert not db.in_transaction()
            return original(*args)

        monkeypatch.setattr(comparison, "comparison_summary", without_sql)
        comparison.attach_plan_vs_fact(PrintsRepository(db), [record])
        assert record["summary"]["actual_hours"] == 4.42


class TestUnlinkedSessions:
    def test_lists_sessions_with_no_card(self, db):
        _session(db, "s_free", machine_min=264.0)

        body = client.get("/prints/unlinked-sessions").json()
        assert [s["session_id"] for s in body["items"]] == ["s_free"]
        assert body["total"] == 1 and body["n_prints"] == 1

    def test_linked_session_disappears_from_the_list(self, db):
        _session(db, "s_taken", machine_min=264.0)
        _record(db, "pr_taken", session_id="s_taken")

        assert client.get("/prints/unlinked-sessions").json()["items"] == []

    def test_preparation_runs_are_listed_but_marked(self, db):
        """Linking one to a print card is rarely right — the operator must see it."""
        _session(db, "s_prep", duration_min=336.0, layers=0,
                 classification="PRE_BURN_SESSION")

        body = client.get("/prints/unlinked-sessions").json()
        assert body["total"] == 1
        assert body["n_prints"] == 0
        assert body["items"][0]["is_print"] is False

    def test_real_prints_sort_ahead_of_preparation_runs(self, db):
        _session(db, "s_prep2", duration_min=1.0, classification="PRE_BURN_SESSION")
        _session(db, "s_real2", machine_min=264.0, classification="REAL_PRINT")

        order = [s["session_id"] for s in client.get("/prints/unlinked-sessions").json()["items"]]
        assert order == ["s_real2", "s_prep2"]
