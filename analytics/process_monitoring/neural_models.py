"""Small dependency-light neural shadow models for compact telemetry.

These models intentionally use NumPy instead of a heavyweight serving runtime.
They are retrained per session, never issue alarms, and must beat a simple
baseline on a chronological holdout before their output is considered useful.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np


def _fit_mlp(
    train_x: np.ndarray,
    train_y: np.ndarray,
    validation_x: np.ndarray,
    validation_y: np.ndarray,
    *,
    hidden: int,
    epochs: int = 350,
    learning_rate: float = 0.01,
    denoising_std: float = 0.0,
    seed: int = 42,
) -> tuple[tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray], float, int]:
    """Train one tanh-hidden/linear-output MLP with Adam and early stopping."""
    rng = np.random.default_rng(seed)
    inputs, outputs = train_x.shape[1], train_y.shape[1]
    w1 = rng.normal(0, math.sqrt(2 / (inputs + hidden)), (inputs, hidden))
    b1 = np.zeros(hidden)
    w2 = rng.normal(0, math.sqrt(2 / (hidden + outputs)), (hidden, outputs))
    b2 = np.zeros(outputs)
    params = [w1, b1, w2, b2]
    first = [np.zeros_like(param) for param in params]
    second = [np.zeros_like(param) for param in params]
    best = tuple(param.copy() for param in params)
    best_loss = math.inf
    best_epoch = 0
    patience = 50

    for epoch in range(1, epochs + 1):
        inputs_batch = train_x
        if denoising_std:
            inputs_batch = train_x + rng.normal(0, denoising_std, train_x.shape)
        hidden_values = np.tanh(inputs_batch @ w1 + b1)
        output = hidden_values @ w2 + b2
        delta_output = 2.0 * (output - train_y) / train_y.size
        delta_hidden = (delta_output @ w2.T) * (1.0 - hidden_values**2)
        gradients = [
            inputs_batch.T @ delta_hidden,
            np.sum(delta_hidden, axis=0),
            hidden_values.T @ delta_output,
            np.sum(delta_output, axis=0),
        ]
        for index, (param, gradient) in enumerate(zip(params, gradients)):
            first[index] = 0.9 * first[index] + 0.1 * gradient
            second[index] = 0.999 * second[index] + 0.001 * gradient**2
            corrected_first = first[index] / (1.0 - 0.9**epoch)
            corrected_second = second[index] / (1.0 - 0.999**epoch)
            param -= learning_rate * corrected_first / (np.sqrt(corrected_second) + 1e-8)
        validation_prediction = np.tanh(validation_x @ w1 + b1) @ w2 + b2
        validation_loss = float(np.mean((validation_prediction - validation_y) ** 2))
        if validation_loss < best_loss - 1e-8:
            best_loss = validation_loss
            best = tuple(param.copy() for param in params)
            best_epoch = epoch
        elif epoch - best_epoch >= patience:
            break
    return best, best_loss, best_epoch


def _predict(x: np.ndarray, model: tuple[np.ndarray, ...]) -> np.ndarray:
    w1, b1, w2, b2 = model
    return np.tanh(x @ w1 + b1) @ w2 + b2


def _aligned(series: dict[str, list[Any]]) -> tuple[list[str], np.ndarray, list[int]]:
    names = [name for name, values in series.items() if isinstance(values, list)]
    if len(names) < 2:
        return [], np.empty((0, 0)), []
    length = min(len(series[name]) for name in names)
    rows, indices = [], []
    for index in range(length):
        row = [series[name][index] for name in names]
        if all(isinstance(value, (int, float)) and math.isfinite(float(value)) for value in row):
            rows.append([float(value) for value in row])
            indices.append(index)
    return names, np.asarray(rows), indices


def neural_telemetry_autoencoder(
    series: dict[str, list[Any]],
    *,
    min_points: int = 80,
) -> dict[str, Any]:
    """Denoising autoencoder with PCA holdout benchmark and anomaly attribution."""
    names, data, indices = _aligned(series)
    if len(data) < min_points or len(names) < 2:
        return {
            "status": "insufficient_data",
            "required_points": min_points,
            "actual_points": len(data),
            "actual_signals": len(names),
        }
    baseline_size = max(40, len(data) // 2)
    baseline = data[:baseline_size]
    centre = np.median(baseline, axis=0)
    scale = np.median(np.abs(baseline - centre), axis=0) * 1.4826
    standard = np.std(baseline, axis=0)
    scale = np.where(scale > 1e-9, scale, np.where(standard > 1e-9, standard, 1.0))
    normalized = (data - centre) / scale
    split = max(25, int(baseline_size * 0.75))
    train, validation = normalized[:split], normalized[split:baseline_size]
    if len(validation) < 8:
        return {"status": "insufficient_data", "actual_points": len(data)}
    hidden = max(1, len(names) // 2)
    model, validation_mse, best_epoch = _fit_mlp(
        train, train, validation, validation,
        hidden=hidden, denoising_std=0.03,
    )
    reconstruction = _predict(normalized, model)
    errors = np.mean((reconstruction - normalized) ** 2, axis=1)

    # Linear PCA is the minimum honest comparator for an autoencoder.
    pca_centre = np.mean(train, axis=0)
    _, _, vh = np.linalg.svd(train - pca_centre, full_matrices=False)
    basis = vh[:hidden]
    pca_validation = (validation - pca_centre) @ basis.T @ basis + pca_centre
    pca_mse = float(np.mean((pca_validation - validation) ** 2))
    gate_passed = validation_mse <= pca_mse * 1.05 + 1e-10

    baseline_errors = errors[:baseline_size]
    error_median = float(np.median(baseline_errors))
    error_mad = float(np.median(np.abs(baseline_errors - error_median)))
    threshold = max(
        float(np.quantile(baseline_errors, 0.99)),
        error_median + 3.5 * 1.4826 * error_mad,
        1e-10,
    )
    candidate_rows = np.flatnonzero(errors[baseline_size:] > threshold) + baseline_size
    ranked = sorted(candidate_rows, key=lambda row: errors[row], reverse=True)[:20]
    anomalies = []
    for row in ranked:
        contributions = (reconstruction[row] - normalized[row]) ** 2
        anomalies.append({
            "index": indices[row],
            "reconstruction_error": round(float(errors[row]), 6),
            "threshold_ratio": round(float(errors[row] / threshold), 3),
            "main_contributing_signal": names[int(np.argmax(contributions))],
        })
    return {
        "status": "ok",
        "mode": "shadow",
        "operator_action_allowed": False,
        "method": "Denoising neural autoencoder (one hidden tanh layer)",
        "signals": names,
        "points": len(data),
        "baseline_points": baseline_size,
        "hidden_units": hidden,
        "training_epochs": best_epoch,
        "validation_mse": round(validation_mse, 7),
        "pca_validation_mse": round(pca_mse, 7),
        "quality_gate_passed": gate_passed,
        "quality_gate_ru": "Ошибка на отложенной части не хуже линейной PCA более чем на 5%",
        "anomaly_threshold": round(threshold, 7),
        "anomaly_count": len(candidate_rows),
        "anomalies": anomalies,
        "limitation_ru": "Начало печати условно принято за норму; модель обучена только на этой печати",
    }


def neural_layer_forecast(values: list[Any], *, window: int = 8, min_points: int = 80) -> dict[str, Any]:
    """One-layer-ahead MLP forecast gated against the persistence baseline."""
    data = np.asarray([
        float(value) for value in values
        if isinstance(value, (int, float)) and math.isfinite(float(value))
    ])
    if len(data) < min_points:
        return {"status": "insufficient_data", "required_points": min_points, "actual_points": len(data)}
    split_point = int(len(data) * 0.7)
    centre = float(np.median(data[:split_point]))
    scale = float(np.median(np.abs(data[:split_point] - centre))) * 1.4826
    scale = scale if scale > 1e-9 else float(np.std(data[:split_point]))
    if scale <= 1e-9:
        return {"status": "insufficient_variation", "actual_points": len(data)}
    normalized = (data - centre) / scale
    x = np.asarray([normalized[index - window:index] for index in range(window, len(data))])
    y = normalized[window:, None]
    split = split_point - window
    train_x, train_y = x[:split], y[:split]
    validation_x, validation_y = x[split:], y[split:]
    if len(validation_x) < 15:
        return {"status": "insufficient_data", "actual_points": len(data)}
    model, validation_mse, best_epoch = _fit_mlp(
        train_x, train_y, validation_x, validation_y,
        hidden=min(12, max(4, window)), learning_rate=0.008,
    )
    predicted = _predict(validation_x, model)[:, 0]
    actual = validation_y[:, 0]
    neural_mae = float(np.mean(np.abs(predicted - actual)))
    persistence_mae = float(np.mean(np.abs(validation_x[:, -1] - actual)))
    gate_passed = neural_mae <= persistence_mae * 0.98
    next_normalized = float(_predict(normalized[-window:][None, :], model)[0, 0])
    next_value = next_normalized * scale + centre
    residuals = np.abs(predicted - actual) * scale
    interval_error = float(np.quantile(residuals, 0.9))
    return {
        "status": "ok",
        "mode": "shadow",
        "operator_action_allowed": False,
        "method": "Autoregressive neural MLP",
        "points": len(data),
        "window": window,
        "training_epochs": best_epoch,
        "forecast_next_layer_sec": round(next_value, 5),
        "forecast_interval_sec": [
            round(next_value - interval_error, 5), round(next_value + interval_error, 5)
        ],
        "validation_mse": round(validation_mse * scale**2, 7),
        "validation_mae_sec": round(neural_mae * scale, 6),
        "persistence_mae_sec": round(persistence_mae * scale, 6),
        "quality_gate_passed": gate_passed,
        "quality_gate_ru": "MAE на будущих слоях минимум на 2% ниже прогноза «как прошлый слой»",
        "limitation_ru": "Прогноз одного слоя не учитывает геометрию детали и команды сканирования",
    }


__all__ = ["neural_layer_forecast", "neural_telemetry_autoencoder"]
