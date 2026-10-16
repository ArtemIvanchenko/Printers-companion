from domain.services.operator_report import build_operator_report
import pytest


def _group() -> dict:
    return {
        "classification": "REAL_PRINT",
        "confidence": 0.9,
        "features": {"pause_count": 1, "data_quality_score": 92},
        "data_quality": {"score": 92, "issues": []},
        "health": {
            "readiness": {"score": 43.0, "factors": {"oxygen": 0.3}},
            "anomalies": [{
                "signal": "SO1",
                "semantic": "кислород",
                "kind": "threshold",
                "severity": "high",
                "value": 2.7,
                "detail": "Кислород выше технологического порога",
            }],
            "burn_drift": {
                "trend": "rising",
                "relative_change_pct": 12.5,
                "slope_sec_per_layer": 0.02,
                "outlier_layers": [{"layer": 140}],
            },
        },
        "soft_sensors": {"metrics": []},
    }


def test_operator_report_prioritizes_confirmed_rejection_and_explains_findings():
    report = build_operator_report(
        session_id="s1",
        group=_group(),
        print_record={"record_id": "p1", "name": "Корпус"},
        quality_outcomes=[{
            "outcome_id": "q1",
            "result": "rejected",
            "inspection_type": "ct",
            "inspection_result": "Поры до 0,8 мм",
            "defect_type": "porosity",
            "defect_location": "верхняя треть детали",
            "created_by": "operator-7",
            "timestamp": "2026-09-03T10:00:00+00:00",
        }],
    )

    assert report["state"]["code"] == "defect_confirmed"
    assert report["quality_outcome"]["result_ru"] == "брак"
    assert report["confidence"]["level_ru"] == "высокая"
    assert any(item["code"].startswith("process:threshold:SO1") for item in report["deviations"])
    assert any("герметич" in item["cause"] for item in report["possible_causes"])
    assert "гипотез" in report["disclaimer"].lower()


def test_operator_report_marks_low_quality_and_does_not_claim_clean_process():
    group = {
        "classification": "REAL_PRINT",
        "confidence": 0.95,
        "features": {"data_quality_score": 31},
        "data_quality": {
            "score": 31,
            "issues": [{
                "kind": "missing_log_family",
                "severity": "high",
                "detail": "Не найден sensors.log",
                "count": 1,
            }],
        },
        "health": {"readiness": {"score": None}, "anomalies": [], "burn_drift": {}},
    }

    report = build_operator_report(session_id="s2", group=group)

    assert report["state"]["code"] == "insufficient_data"
    assert report["confidence"]["level_ru"] == "низкая"
    assert report["deviations"][0]["code"].startswith("data_quality:")
    assert any("предварительными" in item for item in report["recommendations"])


def test_print_card_without_logs_requests_log_linking():
    report = build_operator_report(
        session_id=None,
        group={},
        print_record={"record_id": "p3", "name": "Образец"},
    )

    assert report["state"]["code"] == "not_a_confirmed_print"
    assert any("привязать логи" in item for item in report["recommendations"])


def test_classification_does_not_substitute_for_missing_quality_measurements():
    group = {"classification": "REAL_PRINT", "confidence": .95,
             "data_quality": {"score": None, "grade": "unknown", "issues": []}}
    report = build_operator_report(session_id="s1", group=group)
    assert report["state"]["code"] == "insufficient_data"
    assert any("исходных измерений" in value for value in report["recommendations"])
    accepted = build_operator_report(session_id="s1", group=group,
                                    quality_outcomes=[{"result": "accepted"}])
    assert accepted["state"]["code"] == "accepted"


def test_known_incomplete_telemetry_never_becomes_a_clean_process_conclusion():
    for evidence in [{"complete_available_stream": False, "sample_count": 100},
                     {"sample_count": 0}]:
        group = {"classification": "REAL_PRINT", "confidence": .99,
                 "data_quality": {"score": 100, "issues": []},
                 "health": {"evidence": evidence, "anomalies": []}}
        report = build_operator_report(session_id="s1", group=group)
        assert report["state"]["code"] == "insufficient_data"
        assert report["confidence"]["level_ru"] == "средняя"


def test_explicit_unknown_quality_score_is_not_replaced_with_legacy_feature():
    report = build_operator_report(session_id="s1", group={
        "classification": "REAL_PRINT", "confidence": .99,
        "features": {"data_quality_score": 100}, "data_quality": {"score": None},
    })
    assert report["state"]["code"] == "insufficient_data"
    assert report["confidence"]["data_quality_score"] is None


@pytest.mark.parametrize("classification", ["REAL_PRINT", "REAL_PRINT_WITH_RESUME"])
@pytest.mark.parametrize("completion", [None, "unknown", "finished"])
def test_print_classification_does_not_assert_completion(classification, completion):
    group = {
        "classification": classification, "confidence": .8,
        "data_quality": {"score": 92},
        "features": {"pause_count": 1},
    }
    if completion is not None:
        group["features"]["completion_status"] = completion
    report = build_operator_report(session_id="start-only", group=group)
    assert report["state"]["code"] == "review_recommended"
    text = report["classification"]["label_ru"] + report["state"]["label_ru"]
    assert ("завершена" in text) is (completion == "finished")


def test_confirmed_inspection_is_not_overruled_by_unreliable_log_data():
    group = {
        "classification": "REAL_PRINT", "confidence": .8,
        "data_quality": {"score": 31, "issues": [{
            "kind": "missing_log_family", "severity": "high", "detail": "Нет sensors.log",
        }]},
        "health": {"evidence": {"sample_count": 0, "complete_available_stream": False}},
    }
    accepted = build_operator_report(session_id="s", group=group, quality_outcomes=[{
        "outcome_id": "confirmed-qc", "session_id": "s",
        "is_final": True, "result": "accepted", "inspection_result": "Размеры проверены",
    }])
    assert accepted["state"]["code"] == "accepted_with_observations"
    assert accepted["quality_outcome"]["result"] == "accepted"
    assert accepted["confidence"]["level_ru"] != "высокая"
    assert accepted["deviations"]
    unknown = build_operator_report(session_id="s", group=group)
    assert unknown["state"]["code"] == "insufficient_data"
