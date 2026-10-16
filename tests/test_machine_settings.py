"""Focused regression coverage for manual calibration locks in settings."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes.machine_settings import router, update_machine_params
from domain.models.prints import MachineParams
from storage.db.session import SessionLocal, session_scope
from storage.repositories.prints_repo import PrintsRepository


app = FastAPI()
app.include_router(router)
client = TestClient(app)


def _seed_unlocked_params():
    response = client.put("/settings/machine", json={
        "time_correction_factor": 1.2,
        "recoat_time_ms": 12000,
        "time_correction_by_mat": {"steel@0.060": 1.1},
        "recoat_time_by_mat": {"steel@0.060": 11000},
        "correction_locked": False,
    })
    assert response.status_code == 200
    return response.json()["params"]


@pytest.mark.parametrize("factor,recoat", [(None, None), (1.2, 12000)])
def test_dashboard_save_with_unchanged_corrections_does_not_lock(factor, recoat):
    seeded = client.put("/settings/machine", json={
        "time_correction_factor": factor,
        "recoat_time_ms": recoat,
        "correction_locked": False,
    })
    assert seeded.status_code == 200

    # The dashboard includes both calibration fields even when only a price changes.
    response = client.put("/settings/machine", json={
        "time_correction_factor": factor,
        "recoat_time_ms": recoat,
        "powder_cost_rub_per_kg": 7000,
    })

    assert response.status_code == 200
    assert response.json()["params"]["powder_cost_rub_per_kg"] == 7000
    assert response.json()["params"]["correction_locked"] is False
    assert client.get("/settings/machine").json()["params"]["correction_locked"] is False


def test_first_save_of_empty_calibration_inputs_does_not_lock():
    response = client.put("/settings/machine", json={
        "time_correction_factor": None,
        "recoat_time_ms": None,
        "time_correction_by_mat": {},
        "recoat_time_by_mat": {},
        "powder_cost_rub_per_kg": 7000,
    })
    assert response.status_code == 200
    assert response.json()["params"]["correction_locked"] is False


@pytest.mark.parametrize("field,value", [
    ("time_correction_factor", "1.2"),
    ("recoat_time_ms", "12000"),
    ("time_correction_by_mat", {" steel@0.060 ": "1.1"}),
    ("recoat_time_by_mat", {" steel@0.060 ": "11000"}),
])
def test_normalized_unchanged_calibration_inputs_do_not_lock(field, value):
    _seed_unlocked_params()
    response = client.put("/settings/machine", json={field: value})
    assert response.status_code == 200
    assert response.json()["params"]["correction_locked"] is False


@pytest.mark.parametrize("field,value", [
    ("time_correction_factor", 1.4),
    ("recoat_time_ms", 9000),
    ("time_correction_by_mat", {"steel@0.060": 1.3}),
    ("recoat_time_by_mat", {"steel@0.060": 9000}),
    ("time_correction_factor", None),
    ("recoat_time_by_mat", {}),
])
def test_manual_calibration_change_still_locks(field, value):
    _seed_unlocked_params()
    response = client.put("/settings/machine", json={field: value})
    assert response.status_code == 200
    assert response.json()["params"][field] == value
    assert response.json()["params"]["correction_locked"] is True


@pytest.mark.parametrize("locked", [False, True])
def test_explicit_calibration_lock_takes_precedence(locked):
    _seed_unlocked_params()
    response = client.put("/settings/machine", json={
        "time_correction_factor": 1.4,
        "correction_locked": locked,
    })
    assert response.status_code == 200
    assert response.json()["params"]["correction_locked"] is locked


def test_unchanged_inputs_keep_existing_manual_lock():
    _seed_unlocked_params()
    assert client.put("/settings/machine", json={"correction_locked": True}).status_code == 200
    response = client.put("/settings/machine", json={"time_correction_factor": 1.2})
    assert response.status_code == 200
    assert response.json()["params"]["correction_locked"] is True


def test_comparison_refreshes_params_changed_by_another_writer():
    _seed_unlocked_params()
    with SessionLocal() as stale_db:
        stale_row = stale_db.get(MachineParams, 1)
        assert stale_row.time_correction_factor == 1.2

        with session_scope() as writer_db:
            PrintsRepository(writer_db).save_machine_params({"time_correction_factor": 1.4})

        # A fresh comparison must see 1.4 and recognize 1.2 as a manual change.
        result = update_machine_params(
            {"time_correction_factor": 1.2}, PrintsRepository(stale_db),
        )
        assert result["params"]["correction_locked"] is True
        stale_db.commit()


def test_fractional_laser_count_is_rejected_not_silently_truncated():
    assert client.put("/settings/machine", json={"laser_count": 2}).status_code == 200
    response = client.put("/settings/machine", json={"laser_count": 1.5})
    assert response.status_code == 422
    assert client.get("/settings/machine").json()["params"]["laser_count"] == 2


@pytest.mark.parametrize("value", [2, 2.0, "2"])
def test_whole_laser_count_keeps_supported_numeric_forms(value):
    response = client.put("/settings/machine", json={"laser_count": value})
    assert response.status_code == 200
    assert response.json()["params"]["laser_count"] == 2
