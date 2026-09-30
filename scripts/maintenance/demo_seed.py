"""Seed a clearly synthetic, isolated demo database for the dashboard."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from domain.models import (
    BuildSession, CanonicalEvent, LayerSnapshot,
    Printer, PrinterProfile, PrintRecord, QualityOutcome, MachineParams,
)
from domain.models.quality import Anomaly, MaintenanceRecord, MaterialBatch, PowderUsageCycle, PowderPreparationEvent
from domain.models.sessions import ReportArtifact
from domain.models.insights import PatternInsight, HistoricalAnalysisVerdict, Hypothesis
from operator_journal.journal_entries import build_operator_journal_entry
from storage.db.init_db import create_all
from storage.db.session import SessionLocal
from storage.repositories.runtime import RuntimeRepository


def seed() -> None:
    create_all()
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        if db.query(PrintRecord).filter(PrintRecord.name.like("DEMO ·%")).count():
            print("Demo data already present; leaving it unchanged.")
            return
        profile = PrinterProfile(
            profile_id="demo-m350-profile", vendor="ДЕМО", model_family="M350",
            current_version="demo-1", legacy_names=[], active=True,
        )
        printer = Printer(
            printer_id="demo-m350", name="M350 · демонстрация", vendor="ДЕМО",
            model_family="M350", profile_id=profile.profile_id, serial_number="DEMO-0001",
        )
        db.add_all([profile, printer])
        db.add(MachineParams(
            id=1, hatch_speed_mm_s=900.0, contour_speed_mm_s=650.0,
            hatch_distance_mm=0.10, time_correction_factor=1.0,
            correction_locked=False, layer_thickness_mm=0.06, laser_count=1,
            recoat_time_ms=15000.0, jump_speed_mm_s=2500.0, jump_delay_ms=1.0,
            powder_cost_rub_per_kg=12500.0, gas_cost_rub_per_atm=300.0,
            gas_atm_per_print=8.0, filter_cost_rub=18000.0,
            filter_lifetime_hours=1200.0, platform_cost_rub=2500.0,
            material_densities={"steel": 7.9, "aluminum": 2.7, "titanium": 4.43},
            hatch_speeds_by_mat={"steel": 900.0, "aluminum": 1200.0},
            time_correction_by_mat={}, recoat_time_by_mat={},
            scan_model_by_mat={}, layer_cycle_model_by_mode={}, build_area_cm2=100.0,
        ))
        db.flush()
        owner = "demo-local"
        examples = [
            ("DEMO · Корпус датчика · условно годная", "steel", "accepted", None,
             "Визуальный контроль: поверхность без видимых дефектов. Размеры условно в допуске."),
            ("DEMO · Кронштейн · условный дефект", "steel", "rejected", "porosity",
             "ДЕМО: условная пористость в центральной зоне; требуется КТ для подтверждения."),
            ("DEMO · Теплоотвод · оценка не завершена", "aluminum", "unknown", None,
             "Пример карточки без итогового заключения; контроль не завершён."),
        ]
        records = []
        for index, (name, material, result, defect, inspection) in enumerate(examples):
            start = now - timedelta(days=3-index, hours=5)
            sid = f"demo-session-{index+1}"
            session = BuildSession(
                session_id=sid, origin_compute_node_id=owner, printer_id=printer.printer_id,
                profile_id=profile.profile_id, start_ts=start, end_ts=start+timedelta(hours=4),
                classification="REAL_PRINT", classification_confidence=0.98,
                grouping_confidence=0.95, status="completed",
                analysis_version="demo-synthetic-v1",
                context={"demo": True, "synthetic": True, "material": material,
                         "layer_count": 120, "machine_cycle_hours": 4.0,
                         "explicit_pause_seconds": 300},
            )
            session.context["runtime_payload"] = {
                "group": {
                    "session_id": sid, "classification": "REAL_PRINT",
                    "classification_confidence": 0.98, "synthetic": True,
                    "start_ts": start.isoformat(),
                    "end_ts": (start + timedelta(hours=4)).isoformat(),
                    "analysis_snapshot": {"schema_version": 1, "synthetic": True,
                                          "layer_count": 120, "material": material},
                    "features": {"data_quality_score": 96, "data_quality_grade": "demo",
                                 "layer_count": 120, "explicit_pause_seconds": 300,
                                 "burn_seconds": 8400, "pour_seconds": 1740,
                                 "machine_cycle_seconds": 14400,
                                 "duration_min": 240,
                                 "synthetic": True},
                    "signal_stats": {
                        "chamber_temp_c": {"mean": 26.7, "min": 24, "max": 31, "std": 2.4, "count": 6},
                        "oxygen_ppm": {"mean": 673, "min": 590, "max": 850, "std": 91, "count": 6},
                    },
                    "log_insights": {"synthetic": True, "anomalies": [
                        {"type": "temperature_warning", "severity": "warning", "layer": 61,
                         "message": "Демо-событие: условный рост температуры"},
                        {"type": "recoat_retry", "severity": "warning", "layer": 91,
                         "message": "Демо-событие: условный повтор нанесения порошка"},
                    ]},
                    "telemetry": {"synthetic": True, "time": [0, 1800, 3600, 5400, 7200, 9000],
                                  "signals": {"chamber_temp_c": [24, 25, 27, 31, 28, 25],
                                              "oxygen_ppm": [850, 720, 610, 590, 620, 650]}},
                    "timeline": [], "segments": [],
                }
            }
            record = PrintRecord(
                record_id=f"demo-print-{index+1}", origin_compute_node_id=owner,
                name=name, material=material, layer_thickness_mm=0.06,
                hatch_distance_mm=0.10, session_id=sid, status="completed",
                notes="СИНТЕТИЧЕСКИЕ ДАННЫЕ ДЛЯ ДЕМОНСТРАЦИИ. Не использовать как производственный факт.",
                printed_at=start, powder_cost_rub_per_kg=12500,
                metadata_json={"demo": True, "synthetic": True,
                               "demo_estimate": {"source": "heuristic", "hours": 4.2,
                                                 "quality": "illustrative only"}},
            )
            db.add_all([session, record])
            db.flush()
            records.append((record, session, result, defect, inspection, start))
            for layer in range(1, 121):
                begin = start + timedelta(seconds=(layer-1)*120)
                burn_ms = 70000 + (layer % 7) * 1100
                pour_ms = 14000 + (layer % 3) * 700
                cycle_ms = burn_ms + pour_ms + 16000
                db.add(LayerSnapshot(
                    session_id=sid, layer=layer, ts_start=begin,
                    ts_end=begin+timedelta(milliseconds=cycle_ms),
                    features={"burn_ms": burn_ms, "pour_ms": pour_ms,
                              "make_layer_ms": cycle_ms, "synthetic": True},
                    context={"source": "synthetic_demo_seed", "synthetic": True},
                ))
            for kind, offset, severity, phase, layer in [
                ("print_start", 0, "info", "machine", None),
                ("burn_start", 3600, "info", "laser", 30),
                ("temperature_warning", 7200, "warning", "thermal", 61),
                ("recoat_retry", 10800, "warning", "recoat", 91),
                ("print_complete", 14400, "info", "machine", 120),
            ]:
                ts = start + timedelta(seconds=offset)
                db.add(CanonicalEvent(
                    event_id=f"{sid}-{kind}", session_id=sid, ts=ts,
                    raw_timestamp=ts.isoformat(), ts_uncertainty=0,
                    layer=layer, raw_excerpt=f"[DEMO synthetic] {kind}",
                    subsystem="DEMO", phase=phase, event_type=kind,
                    severity=severity, confidence=0.98,
                    payload={"synthetic": True, "note": "примерное событие, не из лога станка"},
                    evidence_kind="machine_log", provenance=[{"source": "synthetic_demo_seed"}],
                ))
            if result in {"accepted", "rejected"}:
                db.add(QualityOutcome(
                    outcome_id=f"demo-quality-{index+1}", print_record_id=record.record_id,
                    session_id=sid, timestamp=now-timedelta(hours=2-index),
                    inspection_type="visual", result=result, is_final=False,
                    inspection_result=inspection, defect_type=defect,
                    defect_location="центр детали, примерная зона" if defect else None,
                    layer_range={"start": 75, "end": 82} if defect else None,
                    severity="minor" if defect else None,
                    notes="Синтетическая демонстрационная оценка, не результат реального контроля.",
                    created_by="DEMO operator", evidence_links=[{"kind": "demo", "synthetic": True}],
                ))
            else:
                db.add(QualityOutcome(
                    outcome_id=f"demo-quality-{index+1}", print_record_id=record.record_id,
                    session_id=sid, timestamp=now-timedelta(hours=2-index),
                    inspection_type="dimensional", result="unknown", is_final=False,
                    inspection_result=inspection,
                    notes="Демо-запись: контроль не завершён и не является итоговой оценкой.",
                    created_by="DEMO operator", evidence_links=[{"kind": "demo", "synthetic": True}],
                ))
            db.add(Anomaly(
                anomaly_id=f"demo-anomaly-{index+1}", session_id=sid,
                ts_start=start+timedelta(hours=2), ts_end=start+timedelta(hours=2, minutes=4),
                layer_start=61, layer_end=61, anomaly_type="temperature_warning",
                severity="warning", confidence=0.91,
                evidence=[{"source": "synthetic_demo_seed", "synthetic": True}],
                features={"signal": "chamber_temp_c", "value": 31.0, "unit": "°C", "demo": True},
                status="needs_review",
            ))
            db.add(ReportArtifact(
                report_id=f"demo-report-{index+1}", session_id=sid,
                report_type="operator_summary", storage_uri=None, generated_at=now,
                generated_by="synthetic_demo_seed",
                version_metadata={"demo": True, "synthetic": True, "analysis_version": "demo-synthetic-v1"},
                payload={"title": name, "summary": inspection, "synthetic": True,
                         "limitations": ["Нет первичных логов станка", "Параметры и события созданы для демонстрации"]},
            ))
            db.add(Hypothesis(
                hypothesis_id=f"demo-hypothesis-{index+1}", session_id=sid,
                title="Условная связь температурного пика с качеством поверхности",
                description="Синтетическая гипотеза для демонстрации: требуется проверка на реальных данных.",
                relationship="correlates_with", confidence=0.38,
                uncertainty={"synthetic": True, "sample_size": 3},
                supporting_evidence=[{"kind": "demo", "layer": 61}],
                contradictions=[{"kind": "demo", "note": "Выборка создана искусственно"}],
            ))
        runtime = RuntimeRepository(db)
        journal = [
            ("machine_start", "Запуск демонстрационной сборки M350. Камера прогрета, платформа очищена; строка синтетическая."),
            ("powder_change", "Порошок: сталь 316L, партия DEMO-316L-042, сито 63 мкм. Условные параметры для показа формы."),
            ("gas_change", "Аргон: баллон DEMO-AR-01; давление и расход условные, реальное оборудование не подключено."),
            ("operator_note", "На условном слое 61 показано предупреждение температуры; оператор проверил журнал событий (демо)."),
            ("maintenance", "После условного задания очищен recoater, осмотр оптики и фильтра. Демонстрационная запись."),
        ]
        for kind, note in journal:
            event = {
                "event_id": f"demo-event-{kind}", "timestamp": now-timedelta(hours=2),
                "created_at": now, "created_by": "DEMO operator", "source_channel": "web_ui",
                "event_type": kind, "printer_id": printer.printer_id,
                "session_id": records[0][1].session_id if kind != "maintenance" else None,
                "material": "steel", "powder_batch": "DEMO-316L-042" if kind == "powder_change" else None,
                "gas_type": "argon" if kind == "gas_change" else None,
                "component": "recoater" if kind == "maintenance" else None,
                "action": kind, "note": note, "confidence": 0.99,
                "verification_status": "operator_confirmed",
                "linked_machine_events": [],
                "audit_trail": [{"action": "demo_seed", "synthetic": True}],
            }
            runtime.save_operator_event(event)
            entry = build_operator_journal_entry(
                source_channel="web_ui", created_by="DEMO operator", entry_kind=kind,
                raw_text=note, normalized_text=note, status="confirmed",
                project_id="DEMO-2026-001", platform_id="DEMO-M350",
                operator_event_id=event["event_id"],
            )
            runtime.save_operator_journal_entry(entry)
        db.add(MaterialBatch(
            material_batch_id="demo-material-316l", material="steel", alloy="316L",
            batch_code="DEMO-316L-042", supplier="DEMO supplier",
            created_at=now-timedelta(days=20),
            payload={"initial_mass_kg": 12.0, "notes": "Синтетическая демонстрационная партия."},
        ))
        db.add(PowderUsageCycle(
            powder_cycle_id="demo-powder-cycle-316l", material_batch_id="demo-material-316l",
            powder_batch="DEMO-316L-042", reuse_count=2,
            started_at=now-timedelta(days=12), ended_at=None,
            history=[
                {"event": "loaded", "kg": 12.0, "ts": (now-timedelta(days=20)).isoformat()},
                {"event": "consumed", "kg": 1.4, "ts": (now-timedelta(days=8)).isoformat(), "synthetic": True},
                {"event": "consumed", "kg": 1.1, "ts": (now-timedelta(days=3)).isoformat(), "synthetic": True},
            ],
        ))
        db.add(PowderPreparationEvent(
            prep_event_id="demo-sieve-event", powder_cycle_id="demo-powder-cycle-316l",
            timestamp=now-timedelta(days=12), event_type="sieved", value="63", unit="µm",
            payload={"synthetic": True, "note": "Пример подготовки порошка."},
        ))
        db.add_all([
            MaintenanceRecord(maintenance_id="demo-maint-filter", printer_id=printer.printer_id,
                timestamp=now-timedelta(days=2), component="hepa_filter", action="replaced_or_serviced",
                source_event_id="demo-event-maintenance", notes="ДЕМО: условная замена фильтра."),
            MaintenanceRecord(maintenance_id="demo-maint-recoater", printer_id=printer.printer_id,
                timestamp=now-timedelta(days=1), component="recoater_blade", action="inspection",
                source_event_id="demo-event-maintenance", notes="ДЕМО: условный осмотр ракеля."),
            MaintenanceRecord(maintenance_id="demo-maint-glass", printer_id=printer.printer_id,
                timestamp=now-timedelta(days=3), component="protective_glass", action="inspection",
                source_event_id="demo-event-maintenance", notes="ДЕМО: условная проверка оптики."),
            PatternInsight(insight_id="demo-pattern-temp", created_at=now, updated_at=now,
                analysis_window={"from": (now-timedelta(days=7)).isoformat(), "to": now.isoformat()},
                printer_id=printer.printer_id, scope_filters={"synthetic": True},
                insight_type="process_quality", title="Демо: температурный пик на среднем участке",
                description="Иллюстративная гипотеза: на условном слое 61 температура достигает 31 °C. Данные синтетические.",
                supporting_sessions=["demo-session-1", "demo-session-2"],
                supporting_events=["demo-event-operator_note"],
                counterexamples=[{"note": "Нет реального машинного лога."}], sample_size=3,
                effect_size=0.12, confidence=0.38,
                causal_data_quality={"grade": "synthetic_demo", "limitations": ["искусственная выборка"]},
                status="draft", generated_by="synthetic_demo_seed", analysis_version="demo-synthetic-v1",
                recommended_action="Открыть график телеметрии и обсудить, какие данные нужно собирать.",
                audit_trail=[{"action": "demo_seed", "synthetic": True}]),
            HistoricalAnalysisVerdict(verdict_id="demo-review-history", created_at=now,
                analysis_window={"days": 7}, max_iterations=1, completed_iterations=1,
                status="completed", verdict="insufficient_data", confidence=0.2,
                summary="Демонстрационный обзор: синтетических данных недостаточно для производственного вывода.",
                new_insights=["demo-pattern-temp"], updated_insights=[], dismissed_candidates=[],
                counterexamples=[], missing_data=["Исходные логи станка", "Реальные результаты контроля"],
                recommended_actions=["Импортировать подтверждённые логи"],
                affected_sessions=["demo-session-1", "demo-session-2"],
                analysis_version="demo-synthetic-v1", evidence_links=[{"synthetic": True}]),
        ])
        db.commit()
    print("Seeded synthetic demo cards, sessions, layer timings, events, quality observations, and operator journal.")


if __name__ == "__main__":
    seed()
