"""Ищет общие паттерны аномалий across всех *_sensors.log в одной папке.

Прогоняет PyOD (аномальные окна) и ruptures (точки смены поведения) по КАЖДОЙ
печати из папки с логами, затем сводит результат в одну таблицу: где по времени
печати чаще всего концентрируются аномалии, и какие сигналы чаще всего "мусорят".

Запуск:
    python experiments/cross_session_patterns.py [папка с логами]

По умолчанию — ~/Desktop/Логи
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np

from analytics.telemetry_parser import load_aligned_signals as load_aligned

GARBAGE_ABS_THRESHOLD = 1_000_000  # тот же порог, что в parsers/formats/sensors_log.py
COLUMNS = ["SO1", "ST5", "SP4", "Flow T"]


def analyze_session(path: Path, window: int = 60) -> dict:
    from pyod.models.iforest import IForest
    import ruptures as rpt

    aligned = load_aligned(path, COLUMNS)
    n = len(aligned["Time"])
    if n < window * 5:
        return {"path": path, "n_rows": n, "skipped": True}

    # ── PyOD: аномальные окна ────────────────────────────────────────────
    n_windows = n // window
    feats = []
    for w in range(n_windows):
        sl = slice(w * window, (w + 1) * window)
        row: list[float] = []
        for c in COLUMNS:
            seg = aligned[c][sl]
            row += [float(np.nan_to_num(seg.mean())), float(np.nan_to_num(seg.std()))]
        feats.append(row)
    X = np.array(feats)
    clf = IForest(contamination=0.03, random_state=0).fit(X)
    flags = clf.labels_
    flagged_idx = np.where(flags == 1)[0]
    rel_pos = flagged_idx / max(n_windows - 1, 1)  # 0 = начало печати, 1 = конец
    early = int(np.sum(rel_pos < 0.1))
    late = int(np.sum(rel_pos > 0.9))
    middle = int(len(flagged_idx) - early - late)

    # ── ruptures: точки смены поведения температуры ─────────────────────
    st5 = aligned["ST5"]
    step = max(1, len(st5) // 5000)
    x = st5[::step]
    try:
        bkps = rpt.Pelt(model="rbf").fit(x).predict(pen=15)
        n_changepoints = len(bkps) - 1
    except Exception:
        n_changepoints = -1

    # ── "мусорные" значения (переполнение/битый датчик) ──────────────────
    garbage_by_col = {
        c: int(np.sum(np.abs(aligned[c]) > GARBAGE_ABS_THRESHOLD)) for c in COLUMNS
    }

    duration_hours = n / 3600  # логи ~1 Гц
    return {
        "path": path,
        "n_rows": n,
        "duration_hours": round(duration_hours, 2),
        "n_windows": n_windows,
        "n_anomalous_windows": int(len(flagged_idx)),
        "anomaly_pct": round(100 * len(flagged_idx) / max(n_windows, 1), 1),
        "early_anomalies": early,
        "middle_anomalies": middle,
        "late_anomalies": late,
        "changepoints_per_hour": round(n_changepoints / max(duration_hours, 0.01), 2),
        "garbage_by_col": garbage_by_col,
        "skipped": False,
    }


def main() -> None:
    log_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / "Desktop" / "Логи"
    files = sorted(log_dir.glob("*_sensors.log"))
    if not files:
        print(f"Не найдено *_sensors.log в {log_dir}")
        sys.exit(1)

    print(f"Найдено файлов: {len(files)} в {log_dir}\n")
    results = []
    for i, path in enumerate(files, start=1):
        print(f"[{i}/{len(files)}] {path.name} ...", end=" ", flush=True)
        try:
            r = analyze_session(path)
        except Exception as exc:
            print(f"ОШИБКА: {exc}")
            continue
        if r.get("skipped"):
            print("пропущен (слишком короткий файл)")
            continue
        results.append(r)
        print(
            f"{r['duration_hours']}ч, аномальных окон {r['n_anomalous_windows']}"
            f" ({r['anomaly_pct']}%), разладок/ч {r['changepoints_per_hour']}"
        )

    print("\n" + "=" * 90)
    print("СВОДКА ПО ВСЕМ ПЕЧАТЯМ")
    print("=" * 90)

    n = len(results)
    if n == 0:
        print("Нет обработанных файлов.")
        return

    early_dominant = sum(1 for r in results if r["early_anomalies"] >= r["late_anomalies"] and r["early_anomalies"] > 0)
    late_dominant = sum(1 for r in results if r["late_anomalies"] > r["early_anomalies"])
    print(f"\nПечатей обработано: {n}")
    print(f"Аномалии концентрируются в начале печати (первые 10% времени): {early_dominant}/{n} печатей")
    print(f"Аномалии концентрируются в конце печати (последние 10% времени): {late_dominant}/{n} печатей")

    avg_anomaly_pct = np.mean([r["anomaly_pct"] for r in results])
    avg_cp_per_hour = np.mean([r["changepoints_per_hour"] for r in results if r["changepoints_per_hour"] >= 0])
    print(f"\nСреднее % аномальных окон по всем печатям: {avg_anomaly_pct:.1f}%")
    print(f"Среднее число разладок ST5 в час: {avg_cp_per_hour:.2f}")

    print("\nЧастота 'мусорных' значений (|x| > 1e6) по сигналам, где они встречались хотя бы раз:")
    garbage_counts = {c: 0 for c in COLUMNS}
    for r in results:
        for c, cnt in r["garbage_by_col"].items():
            if cnt > 0:
                garbage_counts[c] += 1
    for c, n_files in garbage_counts.items():
        if n_files:
            print(f"  {c}: встречается в {n_files}/{n} печатей")

    print("\nПечати с наибольшей долей аномальных окон (возможные кандидаты на разбор вручную):")
    for r in sorted(results, key=lambda r: -r["anomaly_pct"])[:5]:
        print(f"  {r['path'].name}: {r['anomaly_pct']}% аномальных окон, {r['duration_hours']}ч")


if __name__ == "__main__":
    main()
