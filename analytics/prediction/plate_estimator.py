"""Оценка времени печати всей платформы (детали + поддержки) — главный предсказатель.

============================================================================
БАЗА ЗНАНИЙ О ПРЕДСКАЗАНИИ ВРЕМЕНИ ПЕЧАТИ M-450M / M-350
============================================================================
Этот блок — накопленный опыт, проверенный на реальных печатях цеха
(логи ~/Desktop/Логи, компоновки «Все печати», архив печатей). Он написан
для читателя, который видит проект впервые и не может сам вывести эти
выводы. НЕ удаляйте и не сокращайте его при рефакторинге: каждый пункт
оплачен реальными ошибками в разы.

--- 1. ИЗ ЧЕГО СОСТОИТ ВРЕМЯ ПЕЧАТИ ---
Время печати = СКАН (лазер плавит слой, burn) + НАНЕСЕНИЕ (разравнивание
порошка, pour/recoat) + ПАУЗЫ (простои: оператор, досыпка, перезапуски).

ПРИНЦИП ПРОЕКТА: предсказываем и калибруем ТОЛЬКО машинное время
(скан + нанесение), БЕЗ пауз. Паузы непредсказуемы из геометрии в принципе
(на реальной печати 27-29.05.2026 из 47.6 ч настенного времени 18 ч были
паузами) и должны считаться постфактум отдельной строкой, а не зашиваться
в прогноз. Никогда не калибруйте модель на настенном времени сессии
(end_ts - start_ts) — оно заражено паузами; правильная «правда» — суммы
burn_ms + pour_ms из time_log (см. п.2).

--- 2. ГДЕ ЛЕЖИТ «ПРАВДА» (ground truth) ---
Принтер сам пишет в ``*_time.log`` по КАЖДОМУ физическому слою:
  OLD_STATS: N | pour_ms | burn_ms | make_layer_ms |
Проверено: архивное «время печати» оператора == Σ make_layer_ms с точностью
до сотых на 4 из 5 печатей. Это идеальный эталон машинного времени.
Грабли:
  * Логи ротируются ПО КАЛЕНДАРНЫМ СУТКАМ, не по печатям: печать через
    полночь размазана по файлам разных дат. Признак склейки: последний
    номер слоя файла дня N == первый номер слоя файла дня N+1
    (проверено: 27.05 слои 2..384 -> 28.05 слои 384..950).
  * Частичное покрытие слоёв (обрыв лога) — ВАЛИДНЫЕ точки для регрессии
    по слоям, но НЕ для суммарных сравнений (сумма занижена).
  * Повторный дамп одного слоя — брать первое значение (first-wins).

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
координат, что детали). Тела ниже z=0 — маркеры, не печатаются.
ВАЖНО: компоновка должна соответствовать тому, что РЕАЛЬНО печаталось —
на печати 19.05 в .magics лежали лишние копии деталей (699 см³ против
434 напечатанных), и любой честный расчёт посчитает лишнее.

--- 5. СКОРОСТИ: ПОЧЕМУ ПАСПОРТНЫЕ ЦИФРЫ ВРУТ И ЧТО ДЕЛАТЬ ---
Физика по паспортным скоростям на реальных печатях даёт -54%...-89%:
реальное время слоя включает разгоны, задержки лазера, обработку векторов,
которых нет ни в одном паспорте. Решение — регрессия (scan_calibration):
реальный burn_ms слоя ~ геометрия того же слоя, NNLS.
Два запрета, оба из реальной валидации:
  * Коэффициенты регрессии — НЕ физические скорости. Компоненты геометрии
    коллинеарны (растут вместе с сечением), NNLS распределяет вес между
    ними произвольно. Показывать 1/beta как «скорость» — ложь; работает
    только линейная комбинация целиком.
  * Модель НЕ переносится между режимами: обучение на 0.06 мм и прогноз
    0.025 мм дал R^2 < 0 (хуже среднего). Ключ модели — строго
    «материал@толщина»; чужой режим -> паспортная физика с предупреждением.

--- 6. НАНЕСЕНИЕ (RECOAT) ---
Реальная медиана pour_ms стабильна: ~9.25 с/слой на ВСЕХ материалах и
толщинах (проверено на 6 печатях). Калибруется медианой по материалу
(recoat_calibration -> recoat_time_by_mat), цепочка: калиброванное ->
ручное оператора -> дефолт 9.5 с.

--- 7. ДОСТИГНУТАЯ ТОЧНОСТЬ И КАК ЕЁ УЛУЧШАТЬ ---
Валидация на реальных печатях (машинное время, без пауз):
  04.06 (72 тела, 0.06): физика -54% -> подогнанная R^2=0.941, сумма -0.0%
  27.05 (8 тел, 0.025):  физика -89% -> подогнанная R^2=0.26,  сумма +0.6%
Блочная кросс-валидация: ожидание на БУДУЩЕЙ печати уже откалиброванного
режима ~±10% (худшие краевые блоки ±25%).

КАК СЖИМАЕТСЯ ОТКЛОНЕНИЕ — НАКОПЛЕНИЕМ СТАТИСТИКИ (да, это главный путь):
  * Каждая новая печать режима добавляет сотни-тысячи пар (геометрия слоя,
    реальное время слоя) с ДРУГИМ соотношением компонент (другие детали,
    другая доля поддержек). Разные печати рвут коллинеарность, которую не
    может порвать одна печать, — коэффициенты стабилизируются, краевые
    ±25% схлопываются. Ожидание: 3-5 печатей режима с логами -> устойчивые
    ±5-10%.
  * Механизм УЖЕ автоматический: scan_calibration_report ПУЛИТ слои всех
    привязанных печатей режима и перефитит модель при каждом
    /prints/recalibrate (и при ручной привязке сессии к карточке). Ничего
    включать не надо — только печатать и привязывать логи.
  * Гейты качества не дают мусору попасть в модель: >=150 слоёв,
    сумма in-sample в пределах 15%, R^2-гейт двухканальный (сильный R^2>=0.6
    ИЛИ R^2>=0.2 при сумме <=5% — потому что у плиты с почти постоянным
    сечением дисперсии почти нет и R^2 низкий при идеальной сумме;
    чистый шум даёт R^2~0 и режется).
  * Резервы дальше (по мере роста данных, НЕ делать раньше времени):
    робастная регрессия (Huber) против выбросов первых слоёв; окно
    последних N печатей, если станок стареет и коэффициенты плывут;
    отдельный intercept на печать против дрейфа фокуса между печатями.
  * Контроль: /prints/prediction-accuracy показывает по каждой печати
    прогноз/факт и качество моделей — если ошибка режима систематически
    растёт, станок изменился, нужен recalibrate.
============================================================================

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
from dataclasses import dataclass, field
from typing import Any

from analytics.prediction.layer_engine import (
    LayerGeometrySeries,
    compute_layer_series,
    resolve_scan_model,
    scan_model_key,
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


@dataclass
class BodyEstimate:
    name: str
    kind: str                   # "part" | "support"
    scan_share: float           # approximate share of the joint scan (by boundary length)
    layer_count: int
    height_mm: float
    volume_cm3: float | None    # None for open shells (volume is meaningless)
    warnings: list[str] = field(default_factory=list)


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
    parts_volume_mm3: float = 0.0
    bodies: list[BodyEstimate] = field(default_factory=list)
    geometry_series: LayerGeometrySeries | None = None
    warnings: list[str] = field(default_factory=list)
    prediction: PredictionResult | None = None

    def as_print_time_estimate(self) -> PrintTimeEstimate:
        """Adapter for consumers of the single-part result (cost estimator)."""
        return PrintTimeEstimate(
            scan_hours=self.scan_hours,
            recoat_hours=self.recoat_hours,
            print_hours=self.print_hours,
            total_days=self.total_days,
            method=self.method,
            raw_scan_hours=self.raw_scan_hours,
            raw_recoat_hours=self.raw_recoat_hours,
            raw_print_hours=self.raw_print_hours,
            correction_factor=self.correction_factor,
            breakdown={"recoat_time_ms": round(self.recoat_time_ms, 1),
                      "recoat_time_source": self.recoat_time_source,
                      "scan_source": self.scan_source},
            warnings=list(self.warnings),
            prediction=self.prediction,
        )


def _load_raw_mesh(blob: bytes):
    """Load STL bytes verbatim — no merging, no repair (sheets must survive)."""
    import trimesh

    mesh = trimesh.load(io.BytesIO(blob), file_type="stl", process=False)
    if mesh.is_empty or len(mesh.faces) == 0:
        raise EstimationError("STL не содержит геометрии")
    return mesh


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


# Cache format version: bump if compute_layer_series's output shape changes
# (e.g. a new GEOMETRY_FEATURES entry) so stale rows stop being served instead
# of silently returned as if complete.
_GEOMETRY_CACHE_VERSION = 2


def _geometry_cache_key(
    named: list[tuple[str, bytes, str]], hatch_distance_mm: float,
    layer_thickness_mm: float,
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
    tokens = [f"{kind}:{hashlib.sha256(blob).hexdigest()}" for _, blob, kind in named]
    raw = (
        "|".join(tokens)
        + f"|hatch={hatch_distance_mm:.6f}|layer={layer_thickness_mm:.6f}"
        + f"|v={_GEOMETRY_CACHE_VERSION}"
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def estimate_plate(
    parts: list[tuple[str, bytes]],
    supports: list[tuple[str, bytes]],
    params: dict,
    material: str,
    geometry_cache: Any | None = None,
) -> PlateEstimate:
    """Estimate machine time for the whole plate.

    ``parts``/``supports`` are ``(display_name, stl_bytes)`` pairs in shared
    plate coordinates (Magics exports satisfy this). Raises ``EstimationError``
    when required machine parameters are missing or no body can be estimated.

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
    named = [(name, blob, "part") for name, blob in parts] + [
        (name, blob, "support") for name, blob in supports
    ]
    meshes = []
    metas = []
    parts_volume_mm3 = 0.0
    for name, blob, kind in named:
        mesh = _load_raw_mesh(blob)
        meshes.append(mesh)
        volume = None
        if kind == "part":
            volume = abs(float(mesh.volume))
            parts_volume_mm3 += volume
        metas.append((name, kind, mesh, volume))

    series = None
    cache_key = (
        _geometry_cache_key(named, hatch_distance, thickness)
        if geometry_cache is not None else None
    )
    if cache_key is not None:
        cached = geometry_cache.get_geometry_cache(cache_key)
        if cached is not None:
            series = LayerGeometrySeries.from_snapshot(cached)
            logger.info("plate_estimator: geometry cache hit (%d bodies)", len(meshes))
    if series is None:
        series = compute_layer_series(meshes, hatch_distance, thickness)
        if cache_key is not None:
            geometry_cache.save_geometry_cache(cache_key, series.to_snapshot(), len(meshes))
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
        raw_scan_hours = scan_seconds_from_model(totals, plate_layers, laser_count, model) / 3600.0
        scan_source = "fitted"
        # The fitted model is absolute (trained on real burn seconds) — stacking
        # the blanket correction factor on top would double-correct.
        factor = 1.0
    else:
        raw_scan_hours = _physics_scan_seconds(totals, params, material, laser_count, warnings) / 3600.0
        scan_source = "physics"
        factor = resolve_correction_factor(params, material, thickness)
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
        )
        for i, (name, kind, mesh, volume) in enumerate(metas)
    ]

    return PlateEstimate(
        scan_hours=raw_scan_hours * factor,
        recoat_hours=raw_recoat_hours,
        print_hours=raw_scan_hours * factor + raw_recoat_hours,
        total_days=(raw_scan_hours * factor + raw_recoat_hours) / 24.0,
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
        parts_volume_mm3=parts_volume_mm3,
        bodies=bodies,
        geometry_series=series,
        warnings=warnings,
        prediction=build_time_prediction(
            print_hours=raw_scan_hours * factor + raw_recoat_hours,
            correction_factor=factor,
            scan_source=scan_source,
            recoat_source=recoat_source,
            fitted_model=model if scan_source == "fitted" else None,
            warnings=warnings,
        ),
    )


__all__ = [
    "PlateEstimate", "BodyEstimate", "estimate_plate",
    "resolve_scan_model", "scan_model_key", "scan_seconds_from_model",
]
