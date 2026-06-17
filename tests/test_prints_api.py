"""Tests for the print archive: /prints CRUD, file attachments, /settings/machine."""
import io

import pytest
from fastapi.testclient import TestClient

from api.main import app

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

    def test_put_empty_body_is_422(self):
        assert client.put("/settings/machine", json={"unknown_field": 1}).status_code == 422
