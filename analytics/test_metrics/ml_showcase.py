"""Пробное внедрение внешних ML/статистических библиотек поверх реальных
сигналов сессии — источник данных для вкладки дашборда "Тестовые метрики".

Экспериментальный слой: результаты сюда НЕ идут в основной pipeline
анализа/алармов (rules/engine.py, anomaly/detectors.py остаются как есть).
Каждая секция изолирована — если библиотека упала на конкретных данных,
это не должно ронять остальные ("error" в секции вместо 500-ки на весь эндпоинт).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import numpy as np

from analytics.telemetry_parser import load_aligned_signals
from profiles.signal_catalog import signal_display_name

logger = logging.getLogger(__name__)

_COLUMNS = ["SO1", "ST5", "SP4", "Flow T"]


def _downsample(values: np.ndarray, max_points: int) -> tuple[np.ndarray, int]:
    step = max(1, len(values) // max_points)
    return values[::step], step


def run_ruptures(values: np.ndarray, times: np.ndarray) -> dict[str, Any]:
    import ruptures as rpt

    x, step = _downsample(values, 4000)
    bkps = rpt.Pelt(model="rbf").fit(x).predict(pen=15)
    bkps_ds = bkps[:-1]  # индексы в downsampled-серии
    rows = [min(i * step, len(values) - 1) for i in bkps_ds]

    return {
        "library": "ruptures",
        "title": "Точки смены поведения сигнала (change-point detection)",
        "message": (
            f"Найдено {len(rows)} точек изменения поведения сигнала "
            f"«{signal_display_name('ST5')}» "
            f"— вероятные фазы процесса (нагрев / стабилизация / остывание)."
        ),
        "chart": {
            "type": "line",
            "labels": [str(t) for t in times[::step][: len(x)]],
            "series": [{"label": signal_display_name("ST5"), "data": [round(float(v), 3) for v in x]}],
            "marker_indices": [int(i) for i in bkps_ds],
        },
        "table": {
            "columns": ["Строка", "Время", signal_display_name("ST5")],
            "rows": [[int(i), str(times[i]), round(float(values[i]), 3)] for i in rows[:20]],
        },
    }


def run_pyod(aligned: dict[str, np.ndarray], cols: list[str], window: int = 60) -> dict[str, Any]:
    from pyod.models.iforest import IForest

    n = min(len(aligned[c]) for c in cols)
    n_windows = max(n // window, 1)
    feats: list[list[float]] = []
    win_time: list[str] = []
    for w in range(n_windows):
        sl = slice(w * window, (w + 1) * window)
        row: list[float] = []
        for c in cols:
            seg = aligned[c][sl]
            row += [float(np.nan_to_num(seg.mean())), float(np.nan_to_num(seg.std()))]
        feats.append(row)
        win_time.append(str(aligned["Time"][sl.start]))

    X = np.array(feats)
    clf = IForest(contamination=0.03, random_state=0).fit(X)
    scores, flags = clf.decision_scores_, clf.labels_
    top = np.argsort(scores)[::-1][:10]

    return {
        "library": "PyOD",
        "title": "Аномальные окна сразу по нескольким сигналам (Isolation Forest)",
        "message": (
            f"Из {n_windows} окон по {window} сек помечено аномальными "
            f"{int(flags.sum())} — по совместному поведению {', '.join(cols)}."
        ),
        "chart": {
            "type": "line",
            "labels": win_time,
            "series": [{"label": "anomaly score", "data": [round(float(s), 3) for s in scores]}],
            "marker_indices": [int(i) for i in np.where(flags == 1)[0]],
        },
        "table": {
            "columns": ["Окно", "Время", "Score", *[f"{c}_mean" for c in cols]],
            "rows": [
                [int(i), win_time[i], round(float(scores[i]), 3)]
                + [round(feats[i][2 * j], 3) for j in range(len(cols))]
                for i in top
            ],
        },
    }


def run_tsfresh(values: np.ndarray, name: str, window: int = 60, max_windows: int = 300) -> dict[str, Any]:
    import pandas as pd
    from tsfresh import extract_features
    from tsfresh.feature_extraction import MinimalFCParameters

    n_windows = min(max_windows, max(len(values) // window, 1))
    rows = [
        (w, t, v)
        for w in range(n_windows)
        for t, v in enumerate(values[w * window:(w + 1) * window])
    ]
    df = pd.DataFrame(rows, columns=["id", "time", "value"])
    features = extract_features(
        df,
        column_id="id",
        column_sort="time",
        default_fc_parameters=MinimalFCParameters(),
        disable_progressbar=True,
    )
    cols = list(features.columns)

    return {
        "library": "tsfresh",
        "title": f"Автоматически извлечённые признаки — {name}",
        "message": (
            f"Извлечено {len(cols)} статистических признаков на {n_windows} окон "
            f"по {window} сек — без единой ручной формулы."
        ),
        "chart": None,
        "table": {
            "columns": ["Окно", *cols],
            "rows": [
                [int(i), *[round(float(v), 4) for v in features.iloc[i].tolist()]]
                for i in range(min(10, n_windows))
            ],
        },
    }


def run_lightgbm_shap(values: np.ndarray, name: str, n_lags: int = 10) -> dict[str, Any]:
    """LightGBM, not XGBoost — the project already depends on lightgbm for
    analytics/prediction/defect_risk.py, so this reuses it instead of pulling
    in a second, redundant gradient-boosting library.
    """
    import lightgbm as lgb
    import shap

    X = np.array([values[i - n_lags:i] for i in range(n_lags, len(values))])
    y = values[n_lags:]
    split = int(len(X) * 0.8)
    if split < 10 or len(X) - split < 10:
        raise ValueError("Недостаточно точек для обучения/проверки модели")

    model = lgb.LGBMRegressor(n_estimators=200, max_depth=4, learning_rate=0.05, verbosity=-1)
    model.fit(X[:split], y[:split])
    pred = model.predict(X[split:])
    mae = float(np.mean(np.abs(pred - y[split:])))

    explainer = shap.TreeExplainer(model)
    sample = X[split:split + 200]
    shap_values = explainer.shap_values(sample)
    importance = np.abs(shap_values).mean(axis=0)
    order = np.argsort(importance)[::-1]

    preview = min(150, len(pred))
    return {
        "library": "LightGBM + SHAP",
        "title": f"Прогноз на 1 шаг вперёд + объяснение — {name}",
        "message": f"Средняя ошибка прогноза (MAE) на отложенной выборке: {mae:.4f}.",
        "chart": {
            "type": "line",
            "labels": [str(i) for i in range(preview)],
            "series": [
                {"label": "факт", "data": [round(float(v), 3) for v in y[split:split + preview]]},
                {"label": "прогноз", "data": [round(float(v), 3) for v in pred[:preview]]},
            ],
        },
        "table": {
            "columns": ["Лаг (сек назад)", "Вклад по SHAP"],
            "rows": [[int(n_lags - li), round(float(importance[li]), 4)] for li in order[:5]],
        },
    }


def run_river(
    values: np.ndarray, times: np.ndarray, name: str, top_k: int = 15, max_points: int = 2_000_000
) -> dict[str, Any]:
    """River обрабатывает данные строго по одной точке за раз — чистый Python-цикл,
    без векторизации numpy (в этом и есть смысл "потокового" режима). На файле
    1.28М строк это ≈24 сек — единственная реально медленная часть из всех пяти
    библиотек. Раньше здесь было равномерное прореживание для скорости, но это
    неприемлемо теряло данные: одиночный (не растянутый на много секунд) глитч
    датчика мог попасть между прореженными точками и быть пропущен River начисто
    — а это ровно то, что River должен ловить. Результат кэшируется на уровне
    эндпоинта (api/routes/test_metrics.py), так что эта цена платится один раз
    на сессию, а не при каждом открытии вкладки — поэтому лучше подождать дольше,
    чем незаметно потерять данные. max_points — просто защитный потолок на случай
    аномально огромного файла, в реальных данных проекта не срабатывает.
    """
    from river import anomaly, preprocessing

    values_ds, step = _downsample(values, max_points)
    times_ds = times[::step][: len(values_ds)]

    model = preprocessing.StandardScaler() | anomaly.HalfSpaceTrees(seed=0, window_size=50)
    scores = np.empty(len(values_ds))
    for i, v in enumerate(values_ds):
        scores[i] = model.score_one({"x": float(v)})
        model.learn_one({"x": float(v)})

    warm = min(250, len(values_ds) // 4)
    top = np.argsort(scores[warm:])[::-1][:top_k] + warm
    top = sorted(int(i) for i in top)

    chart_step = max(1, len(values_ds) // 4000)

    def _ru_thousands(n: int) -> str:
        return f"{n:,}".replace(",", " ")

    sample_note = (
        f" Из-за объёма (>{_ru_thousands(max_points)} строк) на вход River взята каждая {step}-я строка "
        f"({_ru_thousands(len(values_ds))} из {_ru_thousands(len(values))}) — единственная библиотека здесь, "
        f"для которой это сделано."
        if step > 1 else ""
    )
    return {
        "library": "River",
        "title": f"Потоковое обнаружение аномалий (онлайн) — {name}",
        "message": (
            f"Обработано {len(values_ds)} точек потоково (по одной, без хранения "
            f"всей истории); выделено {len(top)} самых аномальных.{sample_note}"
        ),
        "chart": {
            "type": "line",
            "labels": [str(t) for t in times_ds[::chart_step]],
            "series": [{"label": name, "data": [round(float(v), 3) for v in values_ds[::chart_step]]}],
            "marker_indices": sorted({i // chart_step for i in top}),
        },
        "table": {
            "columns": ["Строка (в прорежённом ряду)", "Время", "Значение", "Score"],
            "rows": [
                [i, str(times_ds[i]), round(float(values_ds[i]), 4), round(float(scores[i]), 3)]
                for i in top
            ],
        },
    }


def compute_test_metrics(sensors_log_path: str | Path) -> dict[str, Any]:
    """Каждая секция сама решает, нужен ли ей даунсэмплинг (см. run_river) —
    здесь сигналы передаются в полном разрешении, без общего урезания.
    """
    path = Path(sensors_log_path)
    aligned = load_aligned_signals(path, _COLUMNS)
    n = len(aligned["Time"])

    jobs: list[tuple[str, Any]] = [
        ("ruptures", lambda: run_ruptures(aligned["ST5"], aligned["Time"])),
        ("PyOD", lambda: run_pyod(aligned, ["SO1", "ST5", "SP4"])),
        ("tsfresh", lambda: run_tsfresh(aligned["SO1"], signal_display_name("SO1"))),
        ("LightGBM + SHAP", lambda: run_lightgbm_shap(aligned["SO1"], signal_display_name("SO1"))),
        ("River", lambda: run_river(
            aligned["Flow T"], aligned["Time"], signal_display_name("Flow T")
        )),
    ]

    sections: list[dict[str, Any]] = []
    for name, job in jobs:
        try:
            sections.append(job())
        except Exception as exc:  # noqa: BLE001 — одна упавшая библиотека не должна ронять остальные
            logger.warning("test_metrics: %s failed on %s: %s", name, path.name, exc)
            sections.append({"library": name, "error": str(exc)})

    return {
        "session_file": path.name,
        "rows_used": n,
        "sections": sections,
    }
