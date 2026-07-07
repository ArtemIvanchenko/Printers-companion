"""Вкладка дашборда "Тестовые метрики" — результаты внешних ML-библиотек
(ruptures, PyOD, tsfresh, XGBoost+SHAP, River) на реальных сигналах сессии.

Экспериментальный слой поверх analytics/test_metrics — не влияет на основной
pipeline анализа/алармов.
"""
from __future__ import annotations

from collections import OrderedDict
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from analytics.test_metrics import compute_test_metrics
from api.deps.repositories import get_runtime_repository
from domain.enums.common import SourceFileFamily
from storage.repositories.runtime import RuntimeRepository

router = APIRouter(prefix="/test-metrics", tags=["test-metrics"])

_CACHE_MAX = 32
_cache: OrderedDict[str, dict] = OrderedDict()


def _cache_get(session_id: str) -> dict | None:
    if session_id not in _cache:
        return None
    _cache.move_to_end(session_id)
    return _cache[session_id]


def _cache_set(session_id: str, value: dict) -> None:
    _cache[session_id] = value
    _cache.move_to_end(session_id)
    while len(_cache) > _CACHE_MAX:
        _cache.popitem(last=False)


def _find_sensors_log_path(session_id: str, repo: RuntimeRepository) -> Path:
    files = repo.get_session_files(session_id)
    if files is None:
        raise HTTPException(status_code=404, detail="Session not found")
    for f in files:
        if f.classification.family == SourceFileFamily.sensors_log:
            path = Path(f.path)
            if path.exists():
                return path
    raise HTTPException(
        status_code=404,
        detail="У этой сессии нет доступного sensors.log (файл не найден или уже удалён с диска)",
    )


def _latest_session_id(repo: RuntimeRepository) -> str:
    sessions = list(repo.list_session_payloads())
    if not sessions:
        raise HTTPException(status_code=404, detail="Нет ни одной сессии")
    session_id, _payload = max(
        sessions, key=lambda item: (item[1].get("group") or {}).get("start_ts") or ""
    )
    return session_id


@router.get("/latest")
def get_latest_test_metrics(
    refresh: bool = False, repo: RuntimeRepository = Depends(get_runtime_repository)
) -> dict:
    session_id = _latest_session_id(repo)
    return _get_test_metrics(session_id, refresh, repo)


@router.get("/{session_id}")
def get_test_metrics(
    session_id: str, refresh: bool = False, repo: RuntimeRepository = Depends(get_runtime_repository)
) -> dict:
    return _get_test_metrics(session_id, refresh, repo)


def _get_test_metrics(session_id: str, refresh: bool, repo: RuntimeRepository) -> dict:
    if not refresh:
        cached = _cache_get(session_id)
        if cached is not None:
            return cached

    path = _find_sensors_log_path(session_id, repo)
    result = compute_test_metrics(path) | {"session_id": session_id}
    _cache_set(session_id, result)
    return result
