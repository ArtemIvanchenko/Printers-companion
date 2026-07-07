"""Демонстрация ML/статистических библиотек на реальных логах принтера.

Загрузка сигналов переиспользует код из основного проекта
(analytics.telemetry_parser.parse_sensors_log) — это НЕ отдельный парсер,
а прогон реальных сигналов через несколько внешних библиотек, чтобы увидеть,
что каждая из них даёт "из коробки" на настоящих данных.

Библиотеки:
  1. ruptures  — поиск точек изменения поведения сигнала (change-point detection)
  2. PyOD      — обнаружение аномальных окон сразу по нескольким сигналам
  3. tsfresh   — автоматическое извлечение статистических признаков
  4. XGBoost + SHAP — прогноз следующего значения сигнала + объяснение "почему"
  5. River     — потоковое (онлайн) обнаружение аномалий без хранения истории

Запуск (нужно отдельное окружение — см. requirements-demo.txt):
    python experiments/ml_library_showcase.py [путь/к/*_sensors.log]

Если путь не указан — берётся самый маленький *_sensors.log
из ~/Desktop/Логи (для скорости демонстрации).
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np

from analytics.telemetry_parser import load_aligned_signals as load_aligned
from analytics.telemetry_parser import parse_sensors_log  # код основного проекта


def section(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def demo_ruptures(values: np.ndarray, times: np.ndarray, name: str) -> None:
    import ruptures as rpt

    section(f"1. ruptures — поиск разладок в «{name}»")
    step = max(1, len(values) // 5000)
    x = values[::step]
    algo = rpt.Pelt(model="rbf").fit(x)
    bkps = algo.predict(pen=15)
    change_points = [i * step for i in bkps[:-1]]
    print(f"Точек в серии: {len(values)} (после прореживания для скорости: {len(x)})")
    print(f"Найдено точек изменения поведения сигнала: {len(change_points)}")
    for cp in change_points[:10]:
        cp = min(cp, len(values) - 1)
        print(f"  -> строка {cp}, время {times[cp]}, значение {values[cp]:.2f}")
    if len(change_points) > 10:
        print(f"  ... и ещё {len(change_points) - 10}")


def demo_pyod(aligned: dict[str, np.ndarray], cols: list[str], window: int = 60) -> None:
    from pyod.models.iforest import IForest

    section(f"2. PyOD (Isolation Forest) — аномальные окна сразу по {cols}")
    n = min(len(aligned[c]) for c in cols)
    n_windows = n // window
    feats, win_time = [], []
    for w in range(n_windows):
        sl = slice(w * window, (w + 1) * window)
        row: list[float] = []
        for c in cols:
            seg = aligned[c][sl]
            row += [float(seg.mean()), float(seg.std())]
        feats.append(row)
        win_time.append(aligned["Time"][sl.start])
    X = np.array(feats)
    clf = IForest(contamination=0.03, random_state=0).fit(X)
    scores, flags = clf.decision_scores_, clf.labels_

    print(f"Окон по {window} сек: {n_windows}, признаков на окно: {X.shape[1]} (mean/std на каждый сигнал)")
    print(f"Помечено как аномальные: {int(flags.sum())} окон из {n_windows}")
    top = np.argsort(scores)[::-1][:5]
    print("Топ-5 самых аномальных окон:")
    for i in top:
        details = ", ".join(
            f"{c}_mean={feats[i][2 * j]:.3g}" for j, c in enumerate(cols)
        )
        print(f"  -> окно #{i} (~{win_time[i]}), score={scores[i]:.2f}, {details}")


def demo_tsfresh(values: np.ndarray, name: str, window: int = 60, max_windows: int = 300) -> None:
    import pandas as pd
    from tsfresh import extract_features
    from tsfresh.feature_extraction import MinimalFCParameters

    section(f"3. tsfresh — автоматические признаки для «{name}»")
    n_windows = min(max_windows, len(values) // window)
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
    print(f"Окон обработано: {n_windows} (по {window} сек), признаков на окно: {features.shape[1]}")
    print("Примеры признаков:", list(features.columns[:6]))
    print(features.iloc[:3, :5].to_string())


def demo_xgboost_shap(values: np.ndarray, name: str, n_lags: int = 10) -> None:
    import shap
    import xgboost as xgb

    section(f"4. XGBoost + SHAP — прогноз следующего значения «{name}» + объяснение")
    X = np.array([values[i - n_lags:i] for i in range(n_lags, len(values))])
    y = values[n_lags:]
    split = int(len(X) * 0.8)

    model = xgb.XGBRegressor(n_estimators=200, max_depth=4, learning_rate=0.05)
    model.fit(X[:split], y[:split])
    pred = model.predict(X[split:])
    mae = float(np.mean(np.abs(pred - y[split:])))
    print(f"Обучено на {split} точках, проверено на {len(X) - split}")
    print(f"Средняя ошибка прогноза на 1 шаг вперёд (MAE): {mae:.4f}")

    explainer = shap.TreeExplainer(model)
    sample = X[split:split + 200]
    shap_values = explainer.shap_values(sample)
    importance = np.abs(shap_values).mean(axis=0)
    order = np.argsort(importance)[::-1]
    print("Какие прошлые точки сильнее всего влияют на прогноз (по SHAP):")
    for rank, lag_idx in enumerate(order[:5], start=1):
        seconds_ago = n_lags - lag_idx
        print(f"  {rank}. значение {seconds_ago} сек назад -> вклад {importance[lag_idx]:.4f}")


def demo_river(values: np.ndarray, times: np.ndarray, name: str, top_k: int = 10) -> None:
    from river import anomaly, preprocessing

    section(f"5. River — потоковое обнаружение аномалий в «{name}» (без хранения истории)")
    model = preprocessing.StandardScaler() | anomaly.HalfSpaceTrees(seed=0, window_size=50)
    scores = np.empty(len(values))
    for i, v in enumerate(values):
        scores[i] = model.score_one({"x": float(v)})
        model.learn_one({"x": float(v)})

    warm = 250  # даём модели "прогреться", прежде чем доверять её оценкам
    top = np.argsort(scores[warm:])[::-1][:top_k] + warm
    print(f"Обработано точек потоково (по одной, без полного массива в памяти на этапе решения): {len(values)}")
    print(f"Топ-{top_k} самых аномальных точек по онлайн-скору:")
    for i in sorted(top):
        print(f"  -> строка {i}, время {times[i]}, значение {values[i]:.4g}, score={scores[i]:.2f}")


def main() -> None:
    if len(sys.argv) > 1:
        path = Path(sys.argv[1])
    else:
        default_dir = Path.home() / "Desktop" / "Логи"
        candidates = sorted(default_dir.glob("*_sensors.log"), key=lambda p: p.stat().st_size)
        if not candidates:
            print("Не найден ни один *_sensors.log. Укажите путь явным аргументом.")
            sys.exit(1)
        path = candidates[0]  # самый маленький файл — для скорости демо

    print(f"Файл: {path}")

    section("0. Загрузка — analytics.telemetry_parser.parse_sensors_log (код основного проекта)")
    arrays = parse_sensors_log(path)
    print(f"Сигналов распознано: {len(arrays)} -> {sorted(arrays)}")

    aligned = load_aligned(path, ["SO1", "ST5", "SP4", "Flow T"])
    print(f"Синхронизированных по времени строк для многосигнальных демо: {len(aligned['Time'])}")

    demo_ruptures(aligned["ST5"], aligned["Time"], "ST5 (температура)")
    demo_pyod(aligned, ["SO1", "ST5", "SP4"])
    demo_tsfresh(aligned["SO1"], "SO1 (кислород)")
    demo_xgboost_shap(aligned["SO1"], "SO1 (кислород)")
    demo_river(aligned["Flow T"], aligned["Time"], "Flow T (температура газа — содержит выбросы)")

    section("Готово")
    print(f"Все 5 библиотек отработали на реальных данных из {path.name}")


if __name__ == "__main__":
    main()
