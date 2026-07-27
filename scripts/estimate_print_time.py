#!/usr/bin/env python
"""Оценка времени печати по .magics и/или STL — детали и поддержки.

Примеры:
    # Компоновка Magics + экспортированные поддержки
    python scripts/estimate_print_time.py plate.magics s_part1.stl s_part2.stl \
        --layer 0.025 --material steel --hatch-speed 1540 --lasers 2

    # Деталь + её поддержки отдельными STL
    python scripts/estimate_print_time.py part.stl --support s_part.stl --layer 0.06

    # Только модель (будет предупреждение, что поддержек нет)
    python scripts/estimate_print_time.py part.stl --layer 0.06 --material aluminum

Классификация входов:
  *.magics             — читаются печатаемые тела компоновки (детали);
                          встроенные поддержки Magics детектируются, но их
                          геометрия не читается — передайте s_*.stl.
  s_*.stl / --support  — поддержки (открытые оболочки, модель по сечениям).
  прочие *.stl         — детали (PySLM-вектора).

Запуск из корня репозитория: PYTHONPATH=. python scripts/estimate_print_time.py …
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Machine presets, same source as migration 0004 (verified LaserStudio screenshots
# for aluminum; steel figures from the operator's own cost sheets).
_PRESETS = {
    "steel": dict(hatch_speed_mm_s=1000.0, contour_speed_mm_s=430.0,
                  hatch_distance_mm=0.12, jump_speed_mm_s=3000.0, laser_count=1),
    "aluminum": dict(hatch_speed_mm_s=1528.0, contour_speed_mm_s=600.0,
                     hatch_distance_mm=0.12, jump_speed_mm_s=3000.0, laser_count=1),
}


def _classify(paths: list[str], explicit_supports: list[str]) -> tuple[list[Path], list[Path], list[Path]]:
    magics, parts, supports = [], [], []
    for raw in paths:
        p = Path(raw)
        if p.suffix.lower() == ".magics":
            magics.append(p)
        elif p.name.lower().startswith("s_"):
            supports.append(p)
        else:
            parts.append(p)
    supports += [Path(s) for s in explicit_supports]
    return magics, parts, supports


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Оценка времени печати SLM: детали + поддержки",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("inputs", nargs="+", help=".magics и/или .stl файлы (s_* считаются поддержками)")
    ap.add_argument("--support", action="append", default=[], metavar="STL",
                    help="явно пометить файл как поддержку (можно несколько раз)")
    ap.add_argument("--layer", type=float, required=True, help="толщина слоя, мм")
    ap.add_argument("--material", default="steel", choices=sorted(_PRESETS))
    ap.add_argument("--hatch-speed", type=float, help="скорость штриховки, мм/с (иначе пресет)")
    ap.add_argument("--contour-speed", type=float, help="скорость контуров, мм/с")
    ap.add_argument("--hatch-distance", type=float, help="шаг штриховки, мм")
    ap.add_argument("--support-speed", type=float, help="скорость сканирования поддержек, мм/с")
    ap.add_argument("--lasers", type=int, help="число лазеров")
    ap.add_argument("--recoat-ms", type=float, help="время нанесения слоя, мс")
    ap.add_argument("--correction", type=float, default=1.0,
                    help="калибровочный множитель (из истории прогноз/факт)")
    args = ap.parse_args(argv)

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from analytics.prediction.magics_reader import read_plate
    from analytics.prediction.plate_estimator import estimate_plate
    from analytics.prediction.stl_slicer import EstimationError

    preset = _PRESETS[args.material]
    params = {
        "layer_thickness_mm": args.layer,
        "hatch_speed_mm_s": args.hatch_speed or preset["hatch_speed_mm_s"],
        "contour_speed_mm_s": args.contour_speed or preset["contour_speed_mm_s"],
        "hatch_distance_mm": args.hatch_distance or preset["hatch_distance_mm"],
        "jump_speed_mm_s": preset["jump_speed_mm_s"],
        "support_speed_mm_s": args.support_speed,
        "laser_count": args.lasers or preset["laser_count"],
        "recoat_time_ms": args.recoat_ms,
        "time_correction_factor": args.correction,
    }

    magics_files, part_files, support_files = _classify(args.inputs, args.support)
    parts: list[tuple[str, bytes]] = []
    supports: list[tuple[str, bytes]] = []
    pre_warnings: list[str] = []

    for mf in magics_files:
        plate = read_plate(mf)
        pre_warnings.extend(plate.warnings)
        for i, mesh in enumerate(plate.parts):
            parts.append((f"{mf.name}#{i}", mesh.export(file_type="stl")))
        if plate.has_native_supports and not (support_files or supports):
            pass  # предупреждение уже в plate.warnings
    for pf in part_files:
        parts.append((pf.name, pf.read_bytes()))
    for sf in support_files:
        supports.append((sf.name, sf.read_bytes()))

    print(f"Параметры: слой {args.layer} мм · штриховка {params['hatch_speed_mm_s']:.0f} мм/с · "
          f"шаг {params['hatch_distance_mm']} мм · контур {params['contour_speed_mm_s']:.0f} мм/с · "
          f"лазеров {params['laser_count']} · материал {args.material}"
          + (f" · коррекция ×{args.correction}" if args.correction != 1.0 else ""))
    print(f"Тел: {len(parts)} дет. + {len(supports)} подд.\n")

    try:
        est = estimate_plate(parts, supports, params, args.material)
    except EstimationError as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        return 2

    print(f"{'тело':<44}{'тип':>10}{'доля скана':>12}{'слоёв':>7}{'выс,мм':>8}")
    print("-" * 88)
    for b in est.bodies:
        print(f"{b.name[:42]:<44}{b.kind:>10}{b.scan_share*100:>11.1f}%{b.layer_count:>7}{b.height_mm:>8.1f}")
    print("-" * 88)
    print(f"Источник скана: {est.scan_source}"
          + ("" if est.scan_source == "fitted" else " (паспортные скорости — без калибровки по логам)"))
    print(f"Слоёв (плита): {est.layer_count}   высота {est.height_mm:.1f} мм")
    print(f"Сканирование:  {est.scan_hours:.2f} ч")
    print(f"Нанесение:     {est.recoat_hours:.2f} ч")
    print(f"ИТОГО:         {est.print_hours:.2f} ч  ({est.total_days:.2f} сут)")
    if est.correction_factor != 1.0:
        print(f"               (сырая геометрическая оценка {est.raw_print_hours:.2f} ч × {est.correction_factor})")

    all_warnings = pre_warnings + est.warnings
    if all_warnings:
        print("\nПРЕДУПРЕЖДЕНИЯ:")
        for w in all_warnings:
            print(f"  ⚠ {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
