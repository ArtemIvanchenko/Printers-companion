"""
Bulk import of print sessions from Desktop folder into Printer Companion.

Steps:
1. Set machine parameters (M350, calibrated from 23.03 time.log)
2. Register each model folder as a print record
3. Upload primary STL + magics file for each
4. Link to existing log sessions where dates match
5. Run time estimates (fast + accurate)
6. Call prediction-accuracy endpoint and print comparison table
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE = "http://localhost:8000"
MODELS_ROOT = Path(r"C:\Users\Admin\Desktop\Модели с печати")

# ---------- Machine parameters (M350 SLM, calibrated from 23.03 time.log) ----------
# recoat: measured mean from 23.03 log = 9140 ms
# hatch_speed: steel SLM M350 ≈ 900 mm/s (manufacturer spec for stainless)
# hatch_distance: 0.10 mm (100 µm), standard for M350
# contour_speed: 500 mm/s typical
# layer_thickness: 0.04 mm (40 µm) — M350 standard balanced mode
# laser_count: 1 (M350 single-laser)
# jump_speed: 5000 mm/s (SLM standard)
MACHINE_PARAMS = {
    "hatch_speed_mm_s": 900.0,
    "contour_speed_mm_s": 500.0,
    "hatch_distance_mm": 0.10,
    "layer_thickness_mm": 0.04,
    "laser_count": 1,
    "recoat_time_ms": 9140,
    "jump_speed_mm_s": 5000.0,
    "jump_delay_ms": 0.1,
    "hatch_speeds_by_mat": {
        "steel": 900.0,
        "aluminum": 1300.0,
        "titanium": 700.0,
    },
    "material_densities": {
        "steel": 7.9,
        "aluminum": 2.7,
        "titanium": 4.4,
    },
}

# ---------- Print session definitions ----------
# format: (folder_name, display_name, material, notes)
# STL selection: pick one representative non-support STL per session
SESSIONS = [
    {
        "folder": "23.01.2026",
        "sub": r"RS-300\RS-300 60 mkm test 1",
        "name": "RS-300 60мкм — тест 1",
        "material": "steel",
        "notes": "Первый тест на RS-300, шаг слоя 60 мкм",
        "stl": None,  # no STL in folder, only magics
        "magics": "RS-300 60 mkm test 1.magics",
        "date": "2026-01-23",
    },
    {
        "folder": r"04.03.2026 Лопатки + спираль(конечная печать компановка меджикса)",
        "sub": None,
        "name": "Лопатки + спираль (финальная компоновка)",
        "material": "steel",
        "notes": "Лопатки ВИАМ + спираль. Расчёт стоимости прилагается.",
        "stl": "FINGER_1.stl",
        "magics": r"печаь\Spiral new-final.magics",
        "date": "2026-03-04",
    },
    {
        "folder": "2026.03.10",
        "sub": None,
        "name": "Калибровочные образцы 10.03",
        "material": "steel",
        "notes": "Finger + rounded box — калибровочный набор",
        "stl": "finger_cut_1.stl",
        "magics": None,
        "date": "2026-03-10",
    },
    {
        "folder": "2026.03.18 - Spiral",
        "sub": None,
        "name": "Спираль меж.вит.22 внутр.отв.20",
        "material": "steel",
        "notes": "Спираль для SIU, шаг 22 мм, внутр. отверстие 20 мм",
        "stl": "спираль меж вит 22 внутр отв 20.stl",
        "magics": "Spiral.magics",
        "date": "2026-03-18",
    },
    {
        "folder": "18.05.2026",
        "sub": None,
        "name": "Металл. пластина + труба + Al образец 18.05",
        "material": "steel",
        "notes": "Test2_Al(маленькая) — алюминий; пластина и труба — сталь",
        "stl": "Test2_Al(маленькая).STL",
        "magics": "18.05.2025.magics",
        "date": "2026-05-18",
        "stl_material": "aluminum",  # override material for this STL
    },
    {
        "folder": "2026.05.19",
        "sub": None,
        "name": "Труба + пластина + Al образец 19.05",
        "material": "steel",
        "notes": "Уточнённая компоновка от 19.05",
        "stl": "Test2_Al(маленькая).stl",
        "magics": "18.05.2025.magics",
        "date": "2026-05-19",
        "stl_material": "aluminum",
    },
    {
        "folder": "2026.05.25 - 60 Mkm 1Finger + Box",
        "sub": None,
        "name": "Finger + Box 60мкм",
        "material": "steel",
        "notes": "Компоновка: 37 секций finger + 10 боксов, шаг 60 мкм",
        "stl": "finger_cut_1_cut_1_1.stl",
        "magics": "Test60mkm.magics",
        "date": "2026-05-25",
    },
    {
        "folder": "25.05.2026_Siu system",
        "sub": r"25_05 SIU System (SLM)\25_05 SIU System (SLM)",
        "name": "SIU System 25.05 (SLM)",
        "material": "steel",
        "notes": "Кронштейны SIU + тапки для RS-300, компоновка для SLM",
        "stl": "Кронш1 - 4шт.stl",
        "magics": None,
        "date": "2026-05-25",
    },
    {
        "folder": "26.05.2026_RS320",
        "sub": None,
        "name": "RS-320 хвостовики + корпуса 26.05",
        "material": "steel",
        "notes": "Хвостовики 15шт + корпуса 33шт. RS-320",
        "stl": "DKYuG.02.002.451311EMD Korpus - 33sht_16.stl",
        "magics": "RS-320 270526.magics",
        "date": "2026-05-26",
    },
    {
        "folder": "27.05.2026_RS320",
        "sub": None,
        "name": "RS-320 хвостовики + корпуса 27.05",
        "material": "steel",
        "notes": "Финальная компоновка хвостовики 15шт + корпуса 33шт",
        "stl": "DKYuG.02.002.451311EMD Korpus - 33sht_16.stl",
        "magics": "RS-320 270526.magics",
        "date": "2026-05-27",
    },
    {
        "folder": r"03.06.2026 SIU RS-300",
        "sub": None,
        "name": "SIU RS-300 03.06 (компоновка)",
        "material": "steel",
        "notes": "Корпус + тапки Ильи + пришчепки — компоновка для RS-300",
        "stl": "KORPUS_1k2_Alumini.stp.stl",
        "magics": "1.magics",
        "date": "2026-06-03",
    },
    {
        "folder": "04.06.2026 SIU RS-300",
        "sub": None,
        "name": "SIU RS-300 04.06 — держатели + кронштейны",
        "material": "steel",
        "notes": "Держатели СОВ (Тип 1+2) + 4 вида кронштейнов (8+8+8+10шт)",
        "stl": "Держатель СОВ (Тип_1)_1.stl",
        "magics": "Pechat.magics",
        "date": "2026-06-04",
    },
    {
        "folder": "ВИАМ",
        "sub": None,
        "name": "ВИАМ — лопатки (первый и второй проект)",
        "material": "steel",
        "notes": "Лопатки ВИАМ, Magics-компоновка двух проектов",
        "stl": None,
        "magics": "Проект лопаток первый и второй.magics",
        "date": "2026-03-04",
    },
]

# Sessions already in DB with actual layer/time data
# session_id → date string for auto-linking
EXISTING_SESSIONS = {
    "2026-03-23": "session_20260323_4614e084",  # 6842 layers, 5937 min
    "2026-03-27": "session_20260327_ba4a135f",  # 174 layers, 263.7 min
    "2026-04-06": "session_20260406_da8331f1",  # PRE_BURN (no layers)
}


def step(msg: str) -> None:
    print(f"\n{'='*60}\n{msg}\n{'='*60}")


def ok(msg: str) -> None:
    print(f"  [OK] {msg}")


def warn(msg: str) -> None:
    print(f"  [!]  {msg}")


def err(msg: str) -> None:
    print(f"  [ERR] {msg}", file=sys.stderr)


def set_machine_params() -> None:
    step("1. Параметры машины M350")
    r = requests.put(f"{BASE}/settings/machine", json=MACHINE_PARAMS)
    if r.ok:
        ok(f"Параметры установлены. hatch_speed={MACHINE_PARAMS['hatch_speed_mm_s']} mm/s, "
           f"recoat={MACHINE_PARAMS['recoat_time_ms']} ms, lasers={MACHINE_PARAMS['laser_count']}")
    else:
        err(f"Ошибка установки параметров: {r.status_code} {r.text[:200]}")


def create_print_records() -> dict[str, str]:
    """Create all print records; return name → record_id map."""
    step("2. Создание карточек печатей")
    record_map: dict[str, str] = {}

    for s in SESSIONS:
        payload = {
            "name": s["name"],
            "material": s["stl_material"] if s.get("stl_material") else s["material"],
            "notes": s.get("notes") or "",
            "printed_at": s["date"] + "T00:00:00Z",
        }
        r = requests.post(f"{BASE}/prints", json=payload)
        if r.ok:
            rec = r.json()
            rid = rec["record_id"]
            record_map[s["name"]] = rid
            ok(f"{s['name']} → {rid}")
        else:
            err(f"Ошибка создания '{s['name']}': {r.status_code} {r.text[:200]}")

    return record_map


def upload_files(record_map: dict[str, str]) -> None:
    step("3. Загрузка STL и Magics файлов")

    for s in SESSIONS:
        rid = record_map.get(s["name"])
        if not rid:
            continue

        # Resolve base folder
        base = MODELS_ROOT / s["folder"]
        if s.get("sub"):
            file_base = base / s["sub"]
        else:
            file_base = base

        # Upload STL
        stl_name = s.get("stl")
        if stl_name:
            stl_path = file_base / stl_name
            if stl_path.exists():
                data = stl_path.read_bytes()
                r = requests.post(
                    f"{BASE}/prints/{rid}/files",
                    files={"file": (stl_path.name, data, "application/octet-stream")},
                    data={"file_type": "stl"},
                )
                if r.ok:
                    ok(f"STL '{stl_path.name}' → {rid} ({len(data)//1024} KB)")
                else:
                    warn(f"STL upload failed for '{s['name']}': {r.status_code} {r.text[:150]}")
            else:
                warn(f"STL не найден: {stl_path}")

        # Upload magics
        magics_name = s.get("magics")
        if magics_name:
            magics_path = base / magics_name if "\\" not in magics_name else MODELS_ROOT / s["folder"] / magics_name
            if magics_path.exists():
                data = magics_path.read_bytes()
                r = requests.post(
                    f"{BASE}/prints/{rid}/files",
                    files={"file": (magics_path.name, data, "application/octet-stream")},
                    data={"file_type": "magics"},
                )
                if r.ok:
                    ok(f"Magics '{magics_path.name}' → {rid} ({len(data)//1024} KB)")
                else:
                    warn(f"Magics upload failed: {r.status_code} {r.text[:150]}")
            else:
                warn(f"Magics не найден: {magics_path}")


def link_sessions(record_map: dict[str, str]) -> None:
    step("4. Привязка лог-сессий к карточкам")

    for s in SESSIONS:
        rid = record_map.get(s["name"])
        if not rid:
            continue
        date_str = s["date"][:10]  # YYYY-MM-DD

        # Try exact date match first
        session_id = EXISTING_SESSIONS.get(date_str)

        # Also try to match by proximity (model was prepared before print date)
        if not session_id:
            # Look at the log sessions and pick closest one after the model date
            model_date = datetime.fromisoformat(date_str).date()
            best = None
            best_delta = None
            for sess_date, sess_id in EXISTING_SESSIONS.items():
                sd = datetime.fromisoformat(sess_date).date()
                delta = (sd - model_date).days
                if 0 <= delta <= 45:  # print happened 0-45 days after model prep
                    if best_delta is None or delta < best_delta:
                        best_delta = delta
                        best = sess_id
            if best:
                session_id = best

        if session_id:
            r = requests.patch(f"{BASE}/prints/{rid}", json={"session_id": session_id})
            if r.ok:
                ok(f"'{s['name']}' → сессия {session_id}")
            else:
                warn(f"Привязка сессии не удалась для '{s['name']}': {r.text[:150]}")


def run_estimates(record_map: dict[str, str]) -> None:
    step("5. Запуск расчёта времени печати")

    for s in SESSIONS:
        rid = record_map.get(s["name"])
        if not rid or not s.get("stl"):
            if not s.get("stl"):
                warn(f"Пропущено '{s['name']}' — нет STL для расчёта")
            continue

        r = requests.post(f"{BASE}/prints/{rid}/estimate")
        if r.ok:
            pred = r.json().get("prediction", {})
            fast = pred.get("fast", {})
            acc = pred.get("accurate", {})
            ok(f"'{s['name']}': быстро={fast.get('print_hours', '?'):.1f}h, "
               f"точно={acc.get('print_hours', '?'):.1f}h")
        else:
            body = r.json()
            warn(f"Расчёт не удался для '{s['name']}': {r.status_code} {body.get('detail', r.text[:100])}")


def show_accuracy_report() -> None:
    step("6. Сравнение расчётного и фактического времени печати")

    r = requests.get(f"{BASE}/prints/prediction-accuracy")
    if not r.ok:
        err(f"Ошибка получения отчёта: {r.status_code}")
        return

    data = r.json()
    pairs = data.get("pairs", [])

    print(f"\n  Пар расчёт/факт: {data['n_pairs']}")
    if pairs:
        print(f"\n  {'Название':<42} {'Факт, ч':>8} {'Быстро, ч':>10} {'Ошибка%':>8} {'Точно, ч':>10} {'Ошибка%':>8}")
        print(f"  {'-'*90}")
        for p in pairs:
            name = p['name'][:40]
            actual = p.get('actual_hours', 0)
            fast_h = p.get('fast_hours', '-')
            fast_e = p.get('fast_error_pct', '-')
            acc_h = p.get('accurate_hours', '-')
            acc_e = p.get('accurate_error_pct', '-')
            fh = f"{fast_h:.1f}" if isinstance(fast_h, float) else '-'
            fe = f"{fast_e:+.1f}%" if isinstance(fast_e, float) else '-'
            ah = f"{acc_h:.1f}" if isinstance(acc_h, float) else '-'
            ae = f"{acc_e:+.1f}%" if isinstance(acc_e, float) else '-'
            print(f"  {name:<42} {actual:>8.1f} {fh:>10} {fe:>8} {ah:>10} {ae:>8}")

        cf = data.get("suggested_correction_factor")
        em = data.get("excel_median_ratio")
        min_p = data["min_pairs_for_calibration"]
        cf_str = str(cf) if cf else f"need >= {min_p} pairs"
        print(f"\n  Recommended correction_factor (accurate method): {cf_str}")
        print(f"  Медианный коэф. быстрого метода: {em or '—'}")

        if cf:
            print(f"\n  Вывод: 'точный' расчёт {'занижает' if cf > 1 else 'завышает'} время "
                  f"в {cf:.2f}× — применить correction_factor={cf} в настройках машины.")
    else:
        print("  Нет пар для сравнения. Нужно:")
        print("  • Прикрепить STL к карточке")
        print("  • Привязать лог-сессию к карточке (session_id)")
        print("  • Запустить расчёт (/estimate)")

    # Also show all records with estimates even without sessions
    print("\n  --- Все расчёты (включая без лог-сессии) ---")
    recs = requests.get(f"{BASE}/prints?limit=50").json().get("items", [])
    has_estimates = [(r["name"], r.get("metadata_json", {}).get("prediction"))
                     for r in recs if (r.get("metadata_json") or {}).get("prediction")]
    if has_estimates:
        for name, pred in has_estimates:
            fast = pred.get("fast", {})
            acc  = pred.get("accurate", {})
            print(f"  {name[:50]:<52} быстро={fast.get('print_hours', '?'):.1f}h  "
                  f"точно={acc.get('print_hours', '?'):.1f}h  метод={acc.get('method','?')}")
    else:
        print("  Расчётов пока нет.")


def main() -> None:
    print("=" * 60)
    print("  Bulk Import — Printer Companion")
    print(f"  Папка моделей: {MODELS_ROOT}")
    print(f"  Сессий для импорта: {len(SESSIONS)}")
    print("=" * 60)

    # Verify API is running
    try:
        requests.get(f"{BASE}/health", timeout=5).raise_for_status()
    except Exception as e:
        err(f"API недоступен: {e}")
        sys.exit(1)

    set_machine_params()
    record_map = create_print_records()
    print(f"\n  Создано карточек: {len(record_map)}")

    upload_files(record_map)
    link_sessions(record_map)

    # Small pause for background auto-estimate to kick off
    print("\n  Ожидание фоновых расчётов (10 с)...")
    time.sleep(10)

    run_estimates(record_map)
    show_accuracy_report()

    print("\n\nГотово!")


if __name__ == "__main__":
    main()
