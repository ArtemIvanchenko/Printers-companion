"""Оценка нормального машинного времени всей SLM-платформы.

Актуальные правила и проверенные ограничения описаны в ``AGENTS.md`` и
``PLAN_ACCURACY.md``. Этот модуль считает совместную геометрию деталей и
поддержек, фактическое нанесение порошка и нормальный контроллерный цикл.
Операторские остановки, рестарты и повторные попытки в прогноз не входят.

--- 2. ГДЕ ЛЕЖИТ «ПРАВДА» (ground truth) ---
Принтер сам пишет в ``*_time.log`` по КАЖДОМУ физическому слою:
  OLD_STATS: N | pour_ms | burn_ms | make_layer_ms |
``make_layer_ms`` — полный цикл и источник для модели
``max(burn + pour + base_overhead, minimum_cycle)``. Длинные остатки
сохраняются как диагностика, но исключаются из нормального прогноза. Грабли:
  * Логи ротируются ПО КАЛЕНДАРНЫМ СУТКАМ, не по печатям: печать через
    полночь размазана по файлам разных дат. Признак склейки: последний
    номер слоя файла дня N == первый номер слоя файла дня N+1
    (проверено: 27.05 слои 2..384 -> 28.05 слои 384..950).
  * Частичное покрытие слоёв (обрыв лога) — ВАЛИДНЫЕ точки для регрессии
    по слоям, но НЕ для суммарных сравнений (сумма занижена).
  * Эквивалентный повтор на границе суточных логов считать один раз;
    отличающийся повтор — отдельная попытка и неоднозначный слой.

--- 3. ГЕОМЕТРИЯ: КАК СЧИТАТЬ ПРАВИЛЬНО ---
  * Все тела платформы (детали И поддержки) должны хэтчиться СОВМЕСТНО на
    общей оси Z (layer_engine.compute_layer_series). Раздельный расчёт по
    телам теряет перескоки лазера МЕЖДУ телами, а они огромны: на реальном
    слое путь перескоков (16.9 м) БОЛЬШЕ пути штриховки (10.3 м),
    перескоков 1000-1600 на слой.
  * Геометрия сильно меняется по высоте (низ с поддержками ~10x плотнее
    верха). НЕЛЬЗЯ брать среднее по нескольким сечениям и умножать на число
    слоёв — только интеграл послойной серии.
  * trimesh.to_2D() даёт каждому телу СВОЙ 2D-фрейм. Перед объединением
    полигонов разных тел и любыми измерениями расстояний — обратная
    трансформация в общий XY платформы (layer_engine._to_plate_xy).
  * Пересекающиеся контуры (поддержки нарочно входят в деталь на доли мм)
    при хэтчинге гасятся по чётности — обязателен unary_union полигонов
    уровня: перекрытие плавится один раз.
  * Поддержки бывают двух видов: тонкостенные ЗАМКНУТЫЕ (хэтчатся как
    детали) и открытые ЛИСТЫ (только длина одиночного трека, open_mm).
  * MeshFix «чинит» незамкнутые оболочки, ВЫБРАСЫВАЯ геометрию (на реальной
    плите исчезала половина высоты) — поддержки никогда не чинить.

--- 4. ФОРМАТ .MAGICS ---
ZIP с подменённой сигнатурой (MT вместо PK). Вершины: int32 LE, единица
0.1 мкм (делить на 10000 -> мм). Грани: uint32 тройки. Встроенные
поддержки Magics (SupportSurfaces_*) — внутренний формат, НЕ читается;
геометрию поддержек брать из экспортированных s_*.stl (та же система
координат, что детали). Начало физических слоёв нельзя выводить только из
того, что тело поднято: нужен явный datum либо минимум всей предоставленной
печатаемой геометрии с предупреждением.

--- 5. СКОРОСТИ: ПОЧЕМУ ПАСПОРТНЫЕ ЦИФРЫ ВРУТ И ЧТО ДЕЛАТЬ ---
Паспортная физика не включает часть задержек и обработки векторов. Решение —
регрессия (scan_calibration):
реальный burn_ms слоя ~ геометрия того же слоя, NNLS.
Два запрета, оба из реальной валидации:
  * Коэффициенты регрессии — НЕ физические скорости. Компоненты геометрии
    коллинеарны (растут вместе с сечением), NNLS распределяет вес между
    ними произвольно. Показывать 1/beta как «скорость» — ложь; работает
    только линейная комбинация целиком.
  * Модель не переносится между режимами. Ключ — строго
    «материал@толщина»; чужой режим -> паспортная физика с предупреждением.

--- 6. НАНЕСЕНИЕ (RECOAT) ---
Калибруется медианой ``pour_ms`` по материалу
(recoat_calibration -> recoat_time_by_mat), цепочка: калиброванное ->
ручное оператора -> дефолт 9.5 с.

--- 7. ГЕЙТЫ КАЛИБРОВКИ ---
Модель скана требует достаточного числа слоёв и не менее двух независимых
geometry fingerprints; точные повторы одной компоновки держатся в одном CV
fold. Минимальный цикл публикуется только когда и floor-ветвь, и свободная
ветвь наблюдались на нескольких печатях и компоновках и дают существенное
улучшение robust loss. Неидентифицируемый floor хранится как ``None``.

Скан берётся из одного из двух источников:

* **fitted** — модель режима из scan_model_by_mat (см. выше п.5, 7).
  Абсолютная: одеяльный коэффициент поверх НЕ применяется.
* **physics** — паспортные скорости, холодный старт до первой печати
  режима с логами. Одеяльный per-material коэффициент применяется.

Нанесение — один проход на слой платформы, resolve_recoat_ms (п.6).
"""
from __future__ import annotations

import hashlib
import io
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from analytics.prediction.layer_engine import (
    LayerGeometrySeries,
    compute_layer_series,
    machine_mode_key,
    resolve_scan_model,
    scan_model_key,
    scan_seconds_by_layer_from_model,
    scan_seconds_from_model,
)
from analytics.prediction.contract import PredictionResult
from analytics.prediction.print_time import (
    PrintTimeEstimate,
    build_time_prediction,
    resolve_correction_factor,
    resolve_recoat_ms,
)
from analytics.prediction.stl_slicer import EstimationError

logger = logging.getLogger(__name__)

_DEFAULT_JUMP_SPEED_MM_S = 5000.0
MeshSource = bytes | Path


@dataclass
class BodyEstimate:
    name: str
    kind: str                   # "part" | "support"
    scan_share: float           # approximate share of the joint scan (by boundary length)
    layer_count: int
    height_mm: float
    volume_cm3: float | None    # None for open shells (volume is meaningless)
    warnings: list[str] = field(default_factory=list)
    # Absolute build coordinates.  Persisted with the prediction so a later
    # log anomaly can be associated with the STL bodies active at that layer
    # without loading and slicing the models again.
    z_min_mm: float | None = None
    z_max_mm: float | None = None
    x_min_mm: float | None = None
    x_max_mm: float | None = None
    y_min_mm: float | None = None
    y_max_mm: float | None = None
    active_z_intervals_mm: list[list[float]] = field(default_factory=list)


@dataclass
class PlateEstimate:
    scan_hours: float           # after calibration (physics path) / absolute (fitted)
    recoat_hours: float
    print_hours: float
    total_days: float
    raw_scan_hours: float
    raw_recoat_hours: float
    raw_print_hours: float
    correction_factor: float
    layer_count: int            # plate layers (union height)
    height_mm: float
    method: str
    scan_source: str = "physics"          # "fitted" | "physics"
    recoat_time_ms: float = 0.0
    recoat_time_source: str = "default"   # "calibrated" | "manual" | "default"
    # Base controller delay in the calibrated max(base, floor) cycle model.
    # It is not an average effective overhead when minimum_layer_cycle_ms binds.
    layer_overhead_ms: float | None = None
    layer_overhead_source: str = "unavailable"
    layer_overhead_hours: float = 0.0
    layer_overhead_n_prints: int = 0
    layer_overhead_n_layers: int = 0
    layer_cycle_n_geometries: int = 0
    layer_cycle_model_version: str | None = None
    minimum_layer_cycle_ms: float | None = None
    minimum_layer_cycle_status: str = "unavailable"
    minimum_cycle_active_layers: int = 0
    minimum_cycle_training_active_layers: int = 0
    minimum_cycle_training_active_prints: int = 0
    machine_cycle_hours: float = 0.0
    build_origin_z_mm: float = 0.0
    build_origin_source: str = "unknown"
    parts_volume_mm3: float = 0.0
    bodies: list[BodyEstimate] = field(default_factory=list)
    geometry_series: LayerGeometrySeries | None = None
    geometry_totals: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    prediction: PredictionResult | None = None

    def as_print_time_estimate(self) -> PrintTimeEstimate:
        """Adapter for consumers of the single-part result (cost estimator)."""
        return PrintTimeEstimate(
            scan_hours=self.scan_hours,
            recoat_hours=self.recoat_hours,
            print_hours=self.machine_cycle_hours or self.print_hours,
            total_days=(self.machine_cycle_hours or self.print_hours) / 24.0,
            method=self.method,
            raw_scan_hours=self.raw_scan_hours,
            raw_recoat_hours=self.raw_recoat_hours,
            raw_print_hours=self.raw_print_hours + self.layer_overhead_hours,
            correction_factor=self.correction_factor,
            breakdown={"recoat_time_ms": round(self.recoat_time_ms, 1),
                      "recoat_time_source": self.recoat_time_source,
                      "layer_overhead_ms": (
                          round(self.layer_overhead_ms, 1)
                          if self.layer_overhead_ms is not None else None
                      ),
                      "layer_overhead_source": self.layer_overhead_source,
                      "minimum_layer_cycle_ms": self.minimum_layer_cycle_ms,
                      "minimum_cycle_active_layers": self.minimum_cycle_active_layers,
                      "scan_source": self.scan_source},
            warnings=list(self.warnings),
            prediction=self.prediction,
        )


def _load_raw_mesh(source: MeshSource):
    """Load an STL verbatim from bytes or disk (sheets must survive)."""
    import trimesh

    if isinstance(source, Path):
        mesh = trimesh.load(str(source), file_type="stl", process=False)
    else:
        mesh = trimesh.load(io.BytesIO(source), file_type="stl", process=False)
    if mesh.is_empty or len(mesh.faces) == 0:
        raise EstimationError("STL не содержит геометрии")
    return mesh


def _source_sha256(source: MeshSource) -> str:
    """Hash a mesh source with bounded memory for content-addressed caching."""
    digest = hashlib.sha256()
    if isinstance(source, Path):
        with source.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    else:
        digest.update(source)
    return digest.hexdigest()


def _physics_scan_seconds(
    totals: dict[str, float], params: dict, material: str, laser_count: int,
    warnings: list[str],
) -> float:
    """Cold-start scan time from preset speeds over the engine's real geometry."""
    by_mat = params.get("hatch_speeds_by_mat") or {}
    hatch_speed = float(by_mat.get(material) or params["hatch_speed_mm_s"])
    contour_speed = float(params.get("contour_speed_mm_s") or hatch_speed)
    support_speed = float(params.get("support_speed_mm_s") or hatch_speed)
    jump_speed = float(params.get("jump_speed_mm_s") or _DEFAULT_JUMP_SPEED_MM_S)
    jump_delay_s = float(params.get("jump_delay_ms") or 0.0) / 1000.0

    if not params.get("jump_speed_mm_s"):
        warnings.append("Скорость перескока не задана — взято значение по умолчанию.")

    seconds = (
        totals["hatch_mm"] / hatch_speed
        + totals["contour_mm"] / contour_speed
        + totals["open_mm"] / support_speed
        + totals["jump_mm"] / jump_speed
        + totals["n_jumps"] * jump_delay_s
    )
    return seconds / max(laser_count, 1)


def _physics_scan_seconds_by_layer(
    series: LayerGeometrySeries,
    layer_thickness_mm: float,
    params: dict,
    material: str,
    laser_count: int,
) -> list[float]:
    """Cold-start physics burn prediction at each physical layer centre."""
    by_mat = params.get("hatch_speeds_by_mat") or {}
    hatch_speed = float(by_mat.get(material) or params["hatch_speed_mm_s"])
    contour_speed = float(params.get("contour_speed_mm_s") or hatch_speed)
    support_speed = float(params.get("support_speed_mm_s") or hatch_speed)
    jump_speed = float(params.get("jump_speed_mm_s") or _DEFAULT_JUMP_SPEED_MM_S)
    jump_delay_s = float(params.get("jump_delay_ms") or 0.0) / 1000.0
    out: list[float] = []
    for index in range(series.layer_count(layer_thickness_mm)):
        z = series.z_min + (index + 0.5) * layer_thickness_mm
        hatch_mm, contour_mm, jump_mm, n_jumps, open_mm = series.at(z)
        out.append((
            hatch_mm / hatch_speed
            + contour_mm / contour_speed
            + open_mm / support_speed
            + jump_mm / jump_speed
            + n_jumps * jump_delay_s
        ) / max(laser_count, 1))
    return out


def _resolve_layer_cycle_model(scan_model: dict | None) -> tuple[dict | None, str]:
    """Validate new max/base/floor contract, then read legacy additive delay."""
    if not isinstance(scan_model, dict):
        return None, "unavailable"
    candidate = scan_model.get("layer_cycle_model")
    if isinstance(candidate, dict) and candidate.get("version") == "max_base_floor_v1":
        base = candidate.get("base_overhead_ms")
        floor = candidate.get("minimum_cycle_ms")
        if (
            isinstance(base, (int, float))
            and math.isfinite(float(base))
            and 0.0 <= float(base) <= 10_000.0
            and (
                floor is None
                or (
                    isinstance(floor, (int, float))
                    and math.isfinite(float(floor))
                    and 0.0 < float(floor) <= 120_000.0
                )
            )
        ):
            return {
                **candidate,
                "base_overhead_ms": float(base),
                "minimum_cycle_ms": float(floor) if floor is not None else None,
            }, "calibrated_max_base_floor"

    # Existing fitted models stored one additive median residual.  Keep them
    # usable until fresh calibration upgrades the mode to the nested contract.
    legacy = scan_model.get("layer_overhead_ms")
    if (
        isinstance(legacy, (int, float))
        and math.isfinite(float(legacy))
        and 0.0 <= float(legacy) <= 10_000.0
    ):
        return {
            "version": "legacy_additive_overhead_v0",
            "base_overhead_ms": float(legacy),
            "minimum_cycle_ms": None,
            "n_prints": int(scan_model.get("layer_overhead_n_prints") or 0),
            "n_layers": int(scan_model.get("layer_overhead_n_layers") or 0),
            "n_geometries": 0,
            "minimum_cycle_status": "legacy_not_modelled",
        }, "legacy_additive_overhead"
    return None, "unavailable"


def _resolve_layer_cycle_model_for_mode(
    params: dict,
    material: str,
    layer_thickness_mm: float,
    legacy_scan_model: dict | None,
) -> tuple[dict | None, str]:
    """Independent cycle model for the physical machine, then legacy fallback."""
    models = params.get("layer_cycle_model_by_mode") or {}
    keys: list[str] = []
    printer_id = params.get("printer_id")
    laser_count = int(params.get("laser_count") or 1)
    if printer_id:
        keys.append(machine_mode_key(
            str(printer_id), material, layer_thickness_mm, laser_count,
        ))
    keys.append(scan_model_key(material, layer_thickness_mm))
    for key in keys:
        candidate = models.get(key)
        if not isinstance(candidate, dict):
            continue
        resolved, source = _resolve_layer_cycle_model({"layer_cycle_model": candidate})
        if resolved is not None:
            return resolved, source
    # Models written before the independent registry nested cycle data (or a
    # single additive residual) inside the accepted scan model.
    return _resolve_layer_cycle_model(legacy_scan_model)


def _machine_cycle_from_layers(
    raw_scan_seconds_by_layer: list[float],
    *,
    scan_correction_factor: float,
    recoat_ms: float,
    cycle_model: dict | None,
) -> tuple[float, float, int]:
    """Return full normal cycle seconds, controller overhead and floor hits.

    The scan correction is applied *before* the nonlinear floor.  Summing scan
    first would be wrong: two builds can have the same total scan time but a
    different number of short layers held at the controller's minimum cycle.
    """
    recoat_seconds = recoat_ms / 1000.0
    base_seconds = (
        float(cycle_model.get("base_overhead_ms") or 0.0) / 1000.0
        if cycle_model else 0.0
    )
    floor_seconds = (
        float(cycle_model["minimum_cycle_ms"]) / 1000.0
        if cycle_model and cycle_model.get("minimum_cycle_ms") is not None else None
    )
    total_seconds = 0.0
    overhead_seconds = 0.0
    floor_active_layers = 0
    for raw_scan_seconds in raw_scan_seconds_by_layer:
        scan_seconds = raw_scan_seconds * scan_correction_factor
        base_cycle = scan_seconds + recoat_seconds + base_seconds
        if floor_seconds is not None and floor_seconds > base_cycle:
            cycle_seconds = floor_seconds
            floor_active_layers += 1
        else:
            cycle_seconds = base_cycle
        total_seconds += cycle_seconds
        overhead_seconds += cycle_seconds - scan_seconds - recoat_seconds
    return total_seconds, max(overhead_seconds, 0.0), floor_active_layers


# Cache format version: bump if compute_layer_series's output shape changes
# (e.g. a new GEOMETRY_FEATURES entry) so stale rows stop being served instead
# of silently returned as if complete.
_GEOMETRY_CACHE_VERSION = 3


def _geometry_cache_key(
    named: list[tuple[str, MeshSource, str]], hatch_distance_mm: float,
    layer_thickness_mm: float, build_origin_z_mm: float,
) -> str:
    """Content-addressed key for a plate's LayerGeometrySeries.

    Keyed on each body's checksum (in mesh order, part/support tagged) and
    hatch distance and layer thickness. ``compute_layer_series`` uses thickness
    when positioning boundary samples, so omitting it can return geometry
    computed for another print mode even when the difference is usually small.

    Order matters: two records with the same files attached in a different
    order would (correctly) miss the cache, since LayerGeometrySeries.
    body_boundary_mm is positional over the mesh list. That only costs a
    missed optimization, never a wrong answer — a miss just recomputes.
    """
    tokens = [f"{kind}:{_source_sha256(source)}" for _, source, kind in named]
    raw = (
        "|".join(tokens)
        + f"|hatch={hatch_distance_mm:.6f}|layer={layer_thickness_mm:.6f}"
        + f"|origin_z={build_origin_z_mm:.6f}"
        + f"|v={_GEOMETRY_CACHE_VERSION}"
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def estimate_plate(
    parts: list[tuple[str, MeshSource]],
    supports: list[tuple[str, MeshSource]],
    params: dict,
    material: str,
    geometry_cache: Any | None = None,
) -> PlateEstimate:
    """Estimate machine time for the whole plate.

    ``parts``/``supports`` are ``(display_name, source)`` pairs in shared plate
    coordinates, where ``source`` is either STL ``bytes`` or a local ``Path``
    (Magics exports satisfy this). Raises ``EstimationError`` when required
    machine parameters are missing or no body can be estimated.

    ``geometry_cache``, if given, needs ``get_geometry_cache(key) -> dict | None``
    and ``save_geometry_cache(key, series_json, body_count) -> None`` —
    ``PrintsRepository`` satisfies this directly. Co-hatching a real plate is
    minutes of CPU; skipping it on a hit is the whole point of PLAN_ACCURACY.md
    2.2. Meshes still have to be loaded either way (volume/bounds for
    BodyEstimate), which is cheap next to compute_layer_series.

    A cache hit is not bit-exact versus a fresh computation: the cached form is
    LayerGeometrySeries.to_snapshot(), which rounds to 1 decimal place for
    compact storage (that trade-off predates this cache — it was chosen for the
    calibration snapshot). The resulting error is on the order of 1e-4-1e-3
    relative, well under the ±0.7% noise floor this project already accepts
    from the 90-level sampling grid.
    """
    if not parts and not supports:
        raise EstimationError("Не передано ни одной детали и ни одной поддержки")
    thickness = float(params.get("layer_thickness_mm") or 0)
    if thickness <= 0:
        raise EstimationError("Не задана толщина слоя layer_thickness_mm (параметры машины)")
    if not params.get("hatch_speed_mm_s"):
        raise EstimationError("Не задана скорость штриховки (параметры машины)")
    hatch_distance = float(params.get("hatch_distance_mm") or 0)
    if hatch_distance <= 0:
        raise EstimationError("Не задан шаг штриховки hatch_distance_mm (параметры машины)")
    laser_count = int(params.get("laser_count") or 0)
    if laser_count < 1:
        raise EstimationError("Не задано количество лазеров (параметры машины)")

    warnings: list[str] = []
    named = [(name, source, "part") for name, source in parts] + [
        (name, source, "support") for name, source in supports
    ]
    meshes = []
    metas = []
    parts_volume_mm3 = 0.0
    for name, source, kind in named:
        mesh = _load_raw_mesh(source)
        meshes.append(mesh)
        volume = None
        if kind == "part":
            volume = abs(float(mesh.volume))
            parts_volume_mm3 += volume
        metas.append((name, kind, mesh, volume))

    geometry_z_min = min(float(mesh.bounds[0][2]) for mesh in meshes)
    explicit_origin = params.get("build_origin_z_mm")
    if explicit_origin is not None:
        try:
            build_origin_z = float(explicit_origin)
        except (TypeError, ValueError) as exc:
            raise EstimationError("build_origin_z_mm должен быть числом") from exc
        if not math.isfinite(build_origin_z):
            raise EstimationError("build_origin_z_mm должен быть конечным числом")
        build_origin_source = "explicit"
    else:
        build_origin_z = geometry_z_min
        build_origin_source = "minimum_supplied_geometry_z"
        if abs(geometry_z_min) > 1e-6:
            warnings.append(
                "Начало печати не подтверждено, поэтому слои считаются от минимального "
                f"Z всей переданной геометрии: {geometry_z_min:.3f} мм. "
                "Для подтверждённой координаты платформы укажите build_origin_z_mm."
            )

    series = None
    cache_key = (
        _geometry_cache_key(named, hatch_distance, thickness, build_origin_z)
        if geometry_cache is not None else None
    )
    if cache_key is not None:
        cached = geometry_cache.get_geometry_cache(cache_key)
        if cached is not None:
            series = LayerGeometrySeries.from_snapshot(cached)
            logger.info("plate_estimator: geometry cache hit (%d bodies)", len(meshes))
    if series is None:
        series = compute_layer_series(
            meshes, hatch_distance, thickness, build_origin_z_mm=build_origin_z,
        )
        if cache_key is not None:
            geometry_cache.save_geometry_cache(cache_key, series.to_snapshot(), len(meshes))
    warnings.extend(series.warnings)
    totals = series.totals(thickness)
    plate_layers = series.layer_count(thickness)

    if not supports:
        warnings.append(
            "Поддержки не переданы — реальная печать без поддержек не бывает, "
            "оценка является НИЖНЕЙ границей времени."
        )
    if totals["open_mm"] > 0:
        warnings.append(
            "Часть поддержек — открытые оболочки: их стенки учтены как одиночные "
            "треки, перескоки между ними не моделируются."
        )

    model = resolve_scan_model(params, material, thickness)
    if model is not None:
        raw_scan_seconds_by_layer = scan_seconds_by_layer_from_model(
            series, thickness, laser_count, model,
        )
        raw_scan_hours = sum(raw_scan_seconds_by_layer) / 3600.0
        scan_source = "fitted"
        # The fitted model is absolute (trained on real burn seconds) — stacking
        # the blanket correction factor on top would double-correct.
        factor = 1.0
    else:
        raw_scan_seconds_by_layer = _physics_scan_seconds_by_layer(
            series, thickness, params, material, laser_count,
        )
        raw_scan_hours = sum(raw_scan_seconds_by_layer) / 3600.0
        scan_source = "physics"
        factor = resolve_correction_factor(params, material, thickness)
        if not params.get("jump_speed_mm_s"):
            warnings.append("Скорость перескока не задана — взято значение по умолчанию.")
        warnings.append(
            "Скан рассчитан по паспортным скоростям (нет откалиброванной модели для "
            f"режима {scan_model_key(material, thickness)}) — точность ограничена; "
            "накопите печати с логами и запустите перекалибровку."
        )

    recoat_ms, recoat_source = resolve_recoat_ms(params, material)
    raw_recoat_hours = plate_layers * recoat_ms / 1000.0 / 3600.0
    if recoat_source == "default":
        warnings.append(
            f"Время нанесения слоя не задано и не откалибровано по логам — используется "
            f"значение по умолчанию {recoat_ms / 1000:.1f} с/слой."
        )

    cycle_model, layer_overhead_source = _resolve_layer_cycle_model_for_mode(
        params, material, thickness, model,
    )
    layer_overhead_ms = (
        float(cycle_model["base_overhead_ms"]) if cycle_model is not None else None
    )
    minimum_layer_cycle_ms = (
        float(cycle_model["minimum_cycle_ms"])
        if cycle_model is not None and cycle_model.get("minimum_cycle_ms") is not None
        else None
    )
    machine_cycle_seconds, layer_overhead_seconds, minimum_cycle_active_layers = (
        _machine_cycle_from_layers(
            raw_scan_seconds_by_layer,
            scan_correction_factor=factor,
            recoat_ms=recoat_ms,
            cycle_model=cycle_model,
        )
    )
    layer_overhead_hours = layer_overhead_seconds / 3600.0
    if cycle_model is None:
        warnings.append(
            "Нормальный полный цикл слоя пока не откалиброван по make_layer_ms; "
            "межфазная задержка контроллера не добавлена."
        )

    raw_total = raw_scan_hours + raw_recoat_hours

    shares = series.body_shares()
    bodies = [
        BodyEstimate(
            name=name,
            kind=kind,
            scan_share=shares[i] if i < len(shares) else 0.0,
            layer_count=max(int((float(mesh.bounds[1][2]) - float(mesh.bounds[0][2])) / thickness + 0.999999), 1),
            height_mm=float(mesh.bounds[1][2]) - float(mesh.bounds[0][2]),
            volume_cm3=volume / 1000.0 if volume is not None else None,
            z_min_mm=float(mesh.bounds[0][2]),
            z_max_mm=float(mesh.bounds[1][2]),
            x_min_mm=float(mesh.bounds[0][0]),
            x_max_mm=float(mesh.bounds[1][0]),
            y_min_mm=float(mesh.bounds[0][1]),
            y_max_mm=float(mesh.bounds[1][1]),
            active_z_intervals_mm=[
                [
                    max(float(low), float(mesh.bounds[0][2])),
                    min(float(high), float(mesh.bounds[1][2])),
                ]
                for low, high in (
                    series.body_active_z_intervals_mm[i]
                    if i < len(series.body_active_z_intervals_mm) else []
                )
                if max(float(low), float(mesh.bounds[0][2]))
                <= min(float(high), float(mesh.bounds[1][2]))
            ],
        )
        for i, (name, kind, mesh, volume) in enumerate(metas)
    ]

    return PlateEstimate(
        scan_hours=raw_scan_hours * factor,
        recoat_hours=raw_recoat_hours,
        print_hours=raw_scan_hours * factor + raw_recoat_hours,
        total_days=machine_cycle_seconds / 3600.0 / 24.0,
        raw_scan_hours=raw_scan_hours,
        raw_recoat_hours=raw_recoat_hours,
        raw_print_hours=raw_total,
        correction_factor=factor,
        layer_count=plate_layers,
        height_mm=series.height_mm,
        method="plate:cohatch" + ("+fitted" if scan_source == "fitted" else ""),
        scan_source=scan_source,
        recoat_time_ms=recoat_ms,
        recoat_time_source=recoat_source,
        layer_overhead_ms=layer_overhead_ms,
        layer_overhead_source=layer_overhead_source,
        layer_overhead_hours=layer_overhead_hours,
        layer_overhead_n_prints=int(
            (cycle_model or {}).get("n_prints")
            or (model or {}).get("layer_overhead_n_prints")
            or 0
        ),
        layer_overhead_n_layers=int(
            (cycle_model or {}).get("n_layers")
            or (model or {}).get("layer_overhead_n_layers")
            or 0
        ),
        layer_cycle_n_geometries=int((cycle_model or {}).get("n_geometries") or 0),
        layer_cycle_model_version=(cycle_model or {}).get("version"),
        minimum_layer_cycle_ms=minimum_layer_cycle_ms,
        minimum_layer_cycle_status=(cycle_model or {}).get(
            "minimum_cycle_status", "unavailable",
        ),
        minimum_cycle_active_layers=minimum_cycle_active_layers,
        minimum_cycle_training_active_layers=int(
            (cycle_model or {}).get("floor_n_layers") or 0,
        ),
        minimum_cycle_training_active_prints=int(
            (cycle_model or {}).get("floor_n_prints") or 0,
        ),
        machine_cycle_hours=machine_cycle_seconds / 3600.0,
        build_origin_z_mm=build_origin_z,
        build_origin_source=build_origin_source,
        parts_volume_mm3=parts_volume_mm3,
        bodies=bodies,
        geometry_series=series,
        geometry_totals=totals,
        warnings=warnings,
        prediction=build_time_prediction(
            print_hours=machine_cycle_seconds / 3600.0,
            correction_factor=factor,
            scan_source=scan_source,
            recoat_source=recoat_source,
            fitted_model=model if scan_source == "fitted" else None,
            warnings=warnings,
        ),
    )


__all__ = [
    "PlateEstimate", "BodyEstimate", "MeshSource", "estimate_plate",
    "resolve_scan_model", "scan_model_key", "scan_seconds_from_model",
]
