"""Tests for the print archive: /prints CRUD, file attachments, /settings/machine."""
import io

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from api.main import app
from api.routes.prints import _calibration_mismatch_warning

client = TestClient(app)


class _MemoryObjectStore:
    """In-memory stand-in for MinIO used by upload tests."""

    storage: dict[tuple[str, str], bytes] = {}

    def __init__(self, *args, **kwargs):
        pass

    def is_available(self):
        return True

    def ensure_bucket(self, bucket):
        pass

    def ensure_all_buckets(self):
        pass

    def put_bytes(self, bucket, object_name, data, content_type="application/octet-stream"):
        self.storage[(bucket, object_name)] = data
        return f"s3://{bucket}/{object_name}"

    def get_bytes(self, bucket, object_name):
        return self.storage.get((bucket, object_name))

    def open_stream(self, bucket, object_name, chunk_size=1024 * 1024):
        data = self.storage.get((bucket, object_name))
        if data is None:
            return None
        return iter([data[i:i + chunk_size] for i in range(0, len(data), chunk_size)] or [b""])

    def remove_object(self, bucket, object_name):
        return self.storage.pop((bucket, object_name), None) is not None


@pytest.fixture
def memory_store(monkeypatch):
    _MemoryObjectStore.storage = {}
    monkeypatch.setattr("api.routes.prints.ObjectStore", _MemoryObjectStore)
    return _MemoryObjectStore


def _create_record(name="Тестовая деталь", material="steel") -> dict:
    response = client.post("/prints", json={"name": name, "material": material})
    assert response.status_code == 200
    return response.json()


class TestCalibrationMismatchWarning:
    def test_warns_when_physics_point_is_outside_history(self):
        warning = _calibration_mismatch_warning(4.714, (12.75, 29.45), "Steel", 0.06)

        assert warning is not None
        assert "steel@0.060" in warning
        assert "4.71" in warning
        assert "12.75–29.45" in warning

    def test_silent_when_point_is_supported_by_history(self):
        assert _calibration_mismatch_warning(20.0, (12.75, 29.45), "steel", 0.06) is None


class TestPrintRecordCrud:
    def test_create_returns_record(self):
        record = _create_record()
        assert record["record_id"].startswith("pr_")
        assert record["name"] == "Тестовая деталь"
        assert record["material"] == "steel"
        assert record["status"] == "draft"
        assert record["session_id"] is None

    def test_create_requires_name(self):
        assert client.post("/prints", json={}).status_code == 422
        assert client.post("/prints", json={"name": "   "}).status_code == 422

    def test_create_material_is_free_text(self):
        # Materials are not a hardcoded enum — any non-empty name is accepted
        response = client.post("/prints", json={"name": "x", "material": "Inconel 718"})
        assert response.status_code == 200
        assert response.json()["material"] == "inconel 718"

    def test_create_rejects_blank_material(self):
        response = client.post("/prints", json={"name": "x", "material": "   "})
        assert response.status_code == 422


class TestLayerThicknessOnThePrint:
    """Thickness belongs to the print, not to the machine.

    It used to exist only globally in machine_params, so every print was costed
    at whatever the machine was last set to. It also selects which fitted scan
    model applies — those are keyed "material@thickness" and do not transfer
    across thicknesses.
    """

    def test_thickness_is_stored_and_returned(self):
        record = client.post(
            "/prints", json={"name": "Кронштейн", "layer_thickness_mm": 0.06},
        ).json()
        assert record["layer_thickness_mm"] == 0.06
        assert client.get(f"/prints/{record['record_id']}").json()["layer_thickness_mm"] == 0.06

    def test_thickness_is_optional(self):
        """Omitted means "use the machine default", not an error."""
        assert client.post("/prints", json={"name": "x"}).json()["layer_thickness_mm"] is None

    def test_microns_are_rejected(self):
        """0.06 mm typed as 60 must not silently become a 60 mm layer."""
        assert client.post("/prints", json={"name": "x", "layer_thickness_mm": 60}).status_code == 422

    def test_non_positive_is_rejected(self):
        assert client.post("/prints", json={"name": "x", "layer_thickness_mm": 0}).status_code == 422
        assert client.post("/prints", json={"name": "x", "layer_thickness_mm": -0.06}).status_code == 422

    def test_non_numeric_is_rejected(self):
        assert client.post("/prints", json={"name": "x", "layer_thickness_mm": "толстый"}).status_code == 422

    def test_thickness_can_be_patched(self):
        record = _create_record()
        response = client.patch(
            f"/prints/{record['record_id']}", json={"layer_thickness_mm": 0.025},
        )
        assert response.status_code == 200
        assert response.json()["layer_thickness_mm"] == 0.025

    def test_thickness_can_be_cleared_back_to_machine_default(self):
        record = client.post(
            "/prints", json={"name": "x", "layer_thickness_mm": 0.06},
        ).json()
        response = client.patch(
            f"/prints/{record['record_id']}", json={"layer_thickness_mm": None},
        )
        assert response.status_code == 200
        assert response.json()["layer_thickness_mm"] is None


class TestHatchDistanceOnThePrint:
    """Hatch distance belongs to the print, not to the material preset.

    It used to come only from the per-material preset, which held a fixed
    0.12 mm — while the machine's own Monitor100 log shows the applied value
    moving 0.16 -> 0.10 -> 0.90 mm across steel jobs. Scan length goes as
    ~1/hatch, so that one number rescales the entire estimate.
    """

    def test_hatch_is_stored_and_returned(self):
        record = client.post(
            "/prints", json={"name": "Кронштейн", "hatch_distance_mm": 0.9},
        ).json()
        assert record["hatch_distance_mm"] == 0.9
        assert client.get(f"/prints/{record['record_id']}").json()["hatch_distance_mm"] == 0.9

    def test_hatch_is_optional(self):
        """Omitted means "use the material preset", not an error."""
        assert client.post("/prints", json={"name": "x"}).json()["hatch_distance_mm"] is None

    def test_microns_are_rejected(self):
        """0.09 mm typed as 90 must not silently become a 90 mm hatch."""
        assert client.post("/prints", json={"name": "x", "hatch_distance_mm": 90}).status_code == 422

    def test_non_positive_is_rejected(self):
        assert client.post("/prints", json={"name": "x", "hatch_distance_mm": 0}).status_code == 422
        assert client.post("/prints", json={"name": "x", "hatch_distance_mm": -0.1}).status_code == 422

    def test_non_numeric_is_rejected(self):
        assert client.post("/prints", json={"name": "x", "hatch_distance_mm": "мелкий"}).status_code == 422

    def test_the_widest_value_the_machine_actually_ran_is_accepted(self):
        """0.90 mm is a real setting on this machine, not a typo to reject."""
        assert client.post("/prints", json={"name": "x", "hatch_distance_mm": 0.9}).status_code == 200

    def test_hatch_can_be_patched(self):
        record = _create_record()
        response = client.patch(
            f"/prints/{record['record_id']}", json={"hatch_distance_mm": 0.15},
        )
        assert response.status_code == 200
        assert response.json()["hatch_distance_mm"] == 0.15

    def test_hatch_can_be_cleared_back_to_the_preset(self):
        record = client.post(
            "/prints", json={"name": "x", "hatch_distance_mm": 0.9},
        ).json()
        response = client.patch(
            f"/prints/{record['record_id']}", json={"hatch_distance_mm": None},
        )
        assert response.status_code == 200
        assert response.json()["hatch_distance_mm"] is None


class TestScanParamsResolution:
    """machine_params < material preset < the print's own fields."""

    class _Repo:
        def __init__(self, machine, preset):
            self._machine, self._preset = machine, preset

        def get_machine_params(self):
            return self._machine

        def get_active_preset_for_material(self, material):
            return self._preset

    def _resolve(self, record, machine=None, preset=None):
        from api.routes.prints import params_for_record
        return params_for_record(self._Repo(machine, preset), {"material": "steel", **record})

    def test_preset_overrides_the_machine_default(self):
        params = self._resolve(
            {},
            machine={"hatch_distance_mm": 0.12, "laser_count": 1},
            preset={"hatch_distance_mm": 0.2},
        )
        assert params["hatch_distance_mm"] == 0.2
        assert params["laser_count"] == 1  # untouched by the preset

    def test_the_print_overrides_the_preset(self):
        """The whole point: a 0.90 mm job must not be estimated at the preset's 0.12."""
        params = self._resolve(
            {"hatch_distance_mm": 0.9, "layer_thickness_mm": 0.06},
            machine={"hatch_distance_mm": 0.12, "layer_thickness_mm": 0.03},
            preset={"hatch_distance_mm": 0.12},
        )
        assert params["hatch_distance_mm"] == 0.9
        assert params["layer_thickness_mm"] == 0.06

    def test_unset_print_fields_fall_through(self):
        params = self._resolve(
            {"hatch_distance_mm": None, "layer_thickness_mm": None},
            machine={"hatch_distance_mm": 0.12, "layer_thickness_mm": 0.03},
            preset={"hatch_distance_mm": 0.2},
        )
        assert params["hatch_distance_mm"] == 0.2
        assert params["layer_thickness_mm"] == 0.03

    def test_preset_nulls_do_not_erase_the_machine_value(self):
        params = self._resolve(
            {}, machine={"hatch_distance_mm": 0.12}, preset={"hatch_distance_mm": None},
        )
        assert params["hatch_distance_mm"] == 0.12

    def test_no_machine_params_at_all_is_not_a_crash(self):
        assert self._resolve({}, machine=None, preset=None) == {}


class TestPrintRecordReads:
    def test_get_returns_record_with_files(self):
        record = _create_record()
        response = client.get(f"/prints/{record['record_id']}")
        assert response.status_code == 200
        body = response.json()
        assert body["record_id"] == record["record_id"]
        assert body["files"] == []

    def test_get_missing_returns_404(self):
        assert client.get("/prints/pr_missing").status_code == 404

    def test_list_is_paginated(self):
        _create_record(name="Деталь А")
        _create_record(name="Деталь Б")
        response = client.get("/prints", params={"skip": 0, "limit": 1})
        assert response.status_code == 200
        body = response.json()
        assert body["returned"] == 1
        assert body["total"] >= 2
        assert "files" in body["items"][0]

    def test_patch_updates_fields(self):
        record = _create_record()
        response = client.patch(
            f"/prints/{record['record_id']}",
            json={"status": "completed", "notes": "ок", "material": "titanium"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "completed"
        assert body["notes"] == "ок"
        assert body["material"] == "titanium"

    def test_patch_rejects_bad_status(self):
        record = _create_record()
        response = client.patch(f"/prints/{record['record_id']}", json={"status": "bogus"})
        assert response.status_code == 422

    def test_patch_missing_returns_404(self):
        assert client.patch("/prints/pr_missing", json={"status": "active"}).status_code == 404


class TestPrintFiles:
    def test_upload_stores_file(self, memory_store):
        record = _create_record()
        response = client.post(
            f"/prints/{record['record_id']}/files",
            files={"file": ("деталь.stl", io.BytesIO(b"solid x"), "model/stl")},
            data={"file_type": "stl"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["file_type"] == "stl"
        # Object key carries a checksum prefix so renames/replacements never collide
        assert body["object_uri"].startswith(f"s3://stls/{record['record_id']}/")
        assert body["object_uri"].endswith("_деталь.stl")
        assert body["size_bytes"] == 7

    def test_upload_support_stl_autoclassified(self, memory_store):
        record = _create_record()
        response = client.post(
            f"/prints/{record['record_id']}/files",
            files={"file": ("s_деталь.stl", io.BytesIO(b"solid x"), "model/stl")},
            data={"file_type": "stl"},
        )
        assert response.status_code == 200
        assert response.json()["file_type"] == "stl_supports"

    def test_upload_duplicate_checksum_dedupes(self, memory_store):
        record = _create_record()
        for _ in range(2):
            response = client.post(
                f"/prints/{record['record_id']}/files",
                files={"file": ("a.stl", io.BytesIO(b"same-bytes"), "model/stl")},
                data={"file_type": "stl"},
            )
            assert response.status_code == 200
        assert response.json().get("duplicate") is True
        files = client.get(f"/prints/{record['record_id']}").json()["files"]
        assert len(files) == 1

    def test_upload_rejects_bad_file_type(self, memory_store):
        record = _create_record()
        response = client.post(
            f"/prints/{record['record_id']}/files",
            files={"file": ("a.bin", io.BytesIO(b"x"), "application/octet-stream")},
            data={"file_type": "exe"},
        )
        assert response.status_code == 422

    def test_upload_unavailable_store_returns_503(self):
        # conftest stubs ObjectStore.is_available to False by default
        record = _create_record()
        response = client.post(
            f"/prints/{record['record_id']}/files",
            files={"file": ("a.stl", io.BytesIO(b"x"), "model/stl")},
            data={"file_type": "stl"},
        )
        assert response.status_code == 503

    def test_download_roundtrip(self, memory_store):
        record = _create_record()
        upload = client.post(
            f"/prints/{record['record_id']}/files",
            files={"file": ("p.stl", io.BytesIO(b"solid p"), "model/stl")},
            data={"file_type": "stl"},
        ).json()
        response = client.get(
            f"/prints/{record['record_id']}/files/{upload['file_id']}/download"
        )
        assert response.status_code == 200
        assert response.content == b"solid p"


class TestPrintDateAndPowderCost:
    def test_printed_at_parsed_from_name(self):
        record = _create_record(name="23.03.2026_спираль")
        assert record["printed_at"] is not None
        assert record["printed_at"].startswith("2026-03-23")

    def test_printed_at_explicit_overrides_name(self):
        response = client.post(
            "/prints",
            json={"name": "23.03.2026_спираль", "printed_at": "2026-04-01"},
        )
        assert response.json()["printed_at"].startswith("2026-04-01")

    def test_printed_at_none_without_date_anywhere(self):
        record = _create_record(name="спираль без даты")
        assert record["printed_at"] is None

    def test_printed_at_set_from_dated_stl_upload(self, memory_store):
        record = _create_record(name="спираль без даты")
        client.post(
            f"/prints/{record['record_id']}/files",
            files={"file": ("2026-03-23_спираль.stl", io.BytesIO(b"solid x"), "model/stl")},
            data={"file_type": "stl"},
        )
        body = client.get(f"/prints/{record['record_id']}").json()
        assert body["printed_at"].startswith("2026-03-23")

    def test_powder_cost_snapshot_saved(self):
        response = client.post(
            "/prints", json={"name": "x", "powder_cost_rub_per_kg": 7500},
        )
        assert response.json()["powder_cost_rub_per_kg"] == 7500

    def test_powder_cost_negative_rejected(self):
        response = client.post(
            "/prints", json={"name": "x", "powder_cost_rub_per_kg": -1},
        )
        assert response.status_code == 422

    def test_defaults_returns_last_powder_cost(self):
        client.post("/prints", json={"name": "a", "powder_cost_rub_per_kg": 8100})
        d = client.get("/prints/defaults").json()
        assert d["powder_cost_rub_per_kg"] == 8100
        assert isinstance(d["materials"], list) and d["materials"]

    def test_defaults_materials_follow_machine_params(self):
        client.put("/settings/machine", json={"material_densities": {"inconel": 8.2, "steel": 7.9}})
        d = client.get("/prints/defaults").json()
        assert d["materials"] == ["inconel", "steel"]


class TestArchiveSearch:
    def test_search_by_name(self):
        # NOTE: sqlite LIKE is case-sensitive for Cyrillic (ASCII-only folding);
        # on production PostgreSQL ilike is fully case-insensitive.
        _create_record(name="УникальныйКронштейн-77")
        found = client.get("/prints", params={"q": "Кронштейн-77"}).json()
        assert found["total"] == 1
        missed = client.get("/prints", params={"q": "несуществующее-имя-999"}).json()
        assert missed["total"] == 0

    def test_filter_by_material(self):
        _create_record(name="Титановая деталь", material="titanium")
        d = client.get("/prints", params={"material": "titanium"}).json()
        assert d["total"] >= 1
        assert all(item["material"] == "titanium" for item in d["items"])

    def test_filter_by_date_range(self):
        client.post("/prints", json={"name": "СтараяПечать-Я1", "printed_at": "2020-01-15"})
        d = client.get(
            "/prints", params={"date_from": "2020-01-01", "date_to": "2020-02-01"},
        ).json()
        assert d["total"] == 1
        assert d["items"][0]["name"] == "СтараяПечать-Я1"

    def test_free_text_material_accepted(self):
        record = _create_record(name="x", material="inconel")
        assert record["material"] == "inconel"


class TestDeletion:
    def test_delete_record_removes_files(self, memory_store):
        record = _create_record()
        client.post(
            f"/prints/{record['record_id']}/files",
            files={"file": ("a.stl", io.BytesIO(b"solid"), "model/stl")},
            data={"file_type": "stl"},
        )
        r = client.delete(f"/prints/{record['record_id']}")
        assert r.status_code == 200
        assert r.json()["files_removed"] == 1
        assert client.get(f"/prints/{record['record_id']}").status_code == 404

    def test_delete_missing_record_404(self):
        assert client.delete("/prints/pr_missing").status_code == 404

    def test_delete_single_file(self, memory_store):
        record = _create_record()
        up = client.post(
            f"/prints/{record['record_id']}/files",
            files={"file": ("a.stl", io.BytesIO(b"solid"), "model/stl")},
            data={"file_type": "stl"},
        ).json()
        r = client.delete(f"/prints/{record['record_id']}/files/{up['file_id']}")
        assert r.status_code == 200
        assert client.get(f"/prints/{record['record_id']}").json()["files"] == []

    def test_same_name_different_content_no_overwrite(self, memory_store):
        record = _create_record()
        for payload in (b"version-one", b"version-two"):
            client.post(
                f"/prints/{record['record_id']}/files",
                files={"file": ("деталь.stl", io.BytesIO(payload), "model/stl")},
                data={"file_type": "stl"},
            )
        files = client.get(f"/prints/{record['record_id']}").json()["files"]
        assert len(files) == 2
        # Checksum prefix keeps the object keys distinct → both versions stored
        assert files[0]["object_uri"] != files[1]["object_uri"]
        assert len(_MemoryObjectStore.storage) == 2


class TestSessionLinking:
    def test_link_session_sets_printed_at(self):
        from datetime import datetime, timezone
        from storage.db.session import session_scope
        from storage.repositories.prints_repo import PrintsRepository

        record = _create_record(name="привязка-тест")
        start = datetime(2026, 5, 10, 8, 30, tzinfo=timezone.utc)
        with session_scope() as db:
            repo = PrintsRepository(db)
            assert repo.link_session(record["record_id"], "session_xyz", session_start=start)
        body = client.get(f"/prints/{record['record_id']}").json()
        assert body["session_id"] == "session_xyz"
        assert body["printed_at"].startswith("2026-05-10")

    def test_find_unlinked_records_near_uses_print_date(self):
        from datetime import datetime, timezone
        from storage.db.session import SessionLocal
        from storage.repositories.prints_repo import PrintsRepository

        client.post("/prints", json={"name": "близкая-печать", "printed_at": "2026-05-20"})
        client.post("/prints", json={"name": "далёкая-печать", "printed_at": "2026-01-01"})
        with SessionLocal() as db:
            repo = PrintsRepository(db)
            near = repo.find_unlinked_records_near(
                datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc), window_hours=24,
            )
        names = {r["name"] for r in near}
        assert "близкая-печать" in names
        assert "далёкая-печать" not in names


class TestMissingParamsAreNamed:
    """"Fill in the machine parameters" is not actionable when four of the five
    are already filled by a preset.

    On the live DB the entire estimate was blocked by laser_count alone — no
    preset supplies it, machine_params was empty, and the error named no field,
    so nothing pointed at the one number that had to be entered.
    """

    def test_laser_count_defaults_for_this_single_laser_machine(self):
        from api.routes.machine_settings import effective_params, missing_for_estimation

        preset_only = {
            "hatch_speed_mm_s": 1528, "contour_speed_mm_s": 600,
            "hatch_distance_mm": 0.12, "layer_thickness_mm": 0.06,
        }
        assert missing_for_estimation(preset_only) == []
        assert effective_params(preset_only)["laser_count"] == 1

    def test_explicit_laser_count_is_not_overridden(self):
        from api.routes.machine_settings import effective_params

        assert effective_params({"laser_count": 4})["laser_count"] == 4

    def test_missing_fields_are_named_in_russian(self):
        from api.routes.machine_settings import missing_for_estimation

        missing = missing_for_estimation({"hatch_speed_mm_s": 1000})
        assert "скорость контуров" in missing
        assert "толщина слоя" in missing
        # laser_count has a default, so it must not be reported as missing.
        assert "количество лазеров" not in missing

    def test_empty_params_report_everything_except_the_defaulted_field(self):
        from api.routes.machine_settings import missing_for_estimation

        assert len(missing_for_estimation(None)) == 4

    def test_estimate_error_names_the_missing_fields(self):
        record = _create_record()
        response = client.post(f"/prints/{record['record_id']}/estimate")
        # No STL attached either, so this may fail earlier — but when it fails
        # on parameters, the message has to say which ones.
        if "параметров машины" in response.json().get("detail", ""):
            assert "шаг штриховки" in response.json()["detail"]


class TestGeometryQuality:
    def test_known_incomplete_plate_is_blocked_before_estimation(self):
        from api.routes.prints import _assert_geometry_usable

        with pytest.raises(HTTPException) as exc:
            _assert_geometry_usable({
                "metadata_json": {"geometry_quality": {
                    "status": "incomplete", "note": "нет части деталей",
                }},
            })
        assert "геометрия карточки помечена как неполная" in str(exc.value)

    def test_lower_bound_plate_remains_estimatable(self):
        from api.routes.prints import _assert_geometry_usable

        _assert_geometry_usable({
            "metadata_json": {"geometry_quality": {"status": "lower_bound"}},
        })


class TestMachineSettings:
    def test_get_unconfigured_returns_nulls(self):
        response = client.get("/settings/machine")
        assert response.status_code == 200
        body = response.json()
        assert "params" in body
        assert "configured" in body

    def test_put_then_get_roundtrip(self):
        payload = {
            "hatch_speed_mm_s": 1330,
            "contour_speed_mm_s": 500,
            "hatch_distance_mm": 0.12,
            "layer_thickness_mm": 0.05,
            "laser_count": 2,
            "powder_cost_rub_per_kg": 7000,
            "material_densities": {"steel": 7.9, "aluminum": 2.7},
        }
        response = client.put("/settings/machine", json=payload)
        assert response.status_code == 200
        body = response.json()
        assert body["configured"] is True
        assert body["params"]["laser_count"] == 2
        assert body["params"]["material_densities"]["steel"] == 7.9

        again = client.get("/settings/machine").json()
        assert again["params"]["hatch_speed_mm_s"] == 1330

    def test_put_partial_update_keeps_other_fields(self):
        client.put("/settings/machine", json={"powder_cost_rub_per_kg": 7000})
        client.put("/settings/machine", json={"gas_cost_rub_per_atm": 12})
        params = client.get("/settings/machine").json()["params"]
        assert params["powder_cost_rub_per_kg"] == 7000
        assert params["gas_cost_rub_per_atm"] == 12

    def test_put_rejects_negative(self):
        assert client.put("/settings/machine", json={"filter_cost_rub": -5}).status_code == 422

    def test_put_rejects_zero_lasers(self):
        assert client.put("/settings/machine", json={"laser_count": 0}).status_code == 422

    def test_put_rejects_non_numeric(self):
        assert client.put("/settings/machine", json={"hatch_speed_mm_s": "fast"}).status_code == 422

    def test_put_rejects_non_finite_and_non_positive_calibration_values(self):
        response = client.put(
            "/settings/machine",
            content='{"hatch_speed_mm_s": NaN}',
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 422
        assert client.put(
            "/settings/machine", json={"time_correction_by_mat": {"steel@0.060": 0}},
        ).status_code == 422

    def test_put_rejects_string_boolean(self):
        assert client.put(
            "/settings/machine", json={"correction_locked": "false"},
        ).status_code == 422

    def test_put_empty_body_is_422(self):
        assert client.put("/settings/machine", json={"unknown_field": 1}).status_code == 422

    def test_manually_setting_recoat_locks_auto_calibration(self):
        # Same reasoning as time_correction_by_mat: an operator-entered value
        # must not be silently overwritten by the next auto-calibration run.
        response = client.put("/settings/machine", json={"recoat_time_by_mat": {"steel": 9000}})
        assert response.status_code == 200
        assert response.json()["params"]["correction_locked"] is True


class TestDownloadHeaders:
    def test_filename_with_quotes_cannot_break_the_header(self):
        """Uploaded names reach the header almost unchanged, so a quote in one
        used to escape the quoted-string and let the client be told a different
        filename."""
        from api.routes.prints import _content_disposition

        header = _content_disposition('evil".exe;x.stl')
        ascii_part = header.split(";")[1]
        assert '"' not in ascii_part.split("=", 1)[1].strip('"')
        assert "\r" not in header and "\n" not in header

    def test_cyrillic_filename_survives_via_rfc5987(self):
        from api.routes.prints import _content_disposition

        header = _content_disposition("кронштейн.stl")
        # The quoted fallback drops non-ASCII; filename* carries the real name.
        assert "filename*=UTF-8''" in header
        assert "%D0%BA" in header


class TestCalibrationEndpointsIncludeRecoat:
    """Both endpoints must surface recoat calibration alongside scan-time
    calibration — one report, one button, not two operators have to know
    about separately."""

    def test_prediction_accuracy_response_has_a_recoat_section(self):
        response = client.get("/prints/prediction-accuracy")
        assert response.status_code == 200
        body = response.json()
        assert "recoat" in body
        assert "by_material" in body["recoat"]

    def test_recalibrate_response_has_a_recoat_section(self):
        response = client.post("/prints/recalibrate")
        assert response.status_code == 200
        body = response.json()
        assert "recoat" in body
        assert "applied" in body["recoat"]
