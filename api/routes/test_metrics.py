"""Вкладка дашборда "Тестовые метрики" — результаты внешних ML-библиотек
(ruptures, PyOD, tsfresh, LightGBM+SHAP, River) на реальных сигналах сессии.

Экспериментальный слой поверх analytics/test_metrics — не влияет на основной
pipeline анализа/алармов.
"""
from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
from threading import Lock

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from analytics.test_metrics import compute_test_metrics
from core.config.settings import get_settings
from core.versioning.provenance import stable_hash
from domain.enums.common import SourceFileFamily
from domain.services.compute_affinity import ComputeAffinityError, require_compute_owner
from domain.services.session_sources import SessionSources, read_session_sources
from storage.db.session import get_db
from storage.repositories.session_reads import SessionReadsRepository

router = APIRouter(prefix="/test-metrics", tags=["test-metrics"])

_CACHE_MAX = 32
_cache: OrderedDict[str, dict] = OrderedDict()
_cache_lock = Lock()


def _cache_get(key: str) -> dict | None:
    with _cache_lock:
        if key not in _cache:
            return None
        _cache.move_to_end(key)
        return _cache[key]


def _cache_set(key: str, value: dict) -> None:
    with _cache_lock:
        _cache[key] = value
        _cache.move_to_end(key)
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)


def _find_sensors_log_path(sources: SessionSources) -> tuple[Path, str]:
    for f in sources.files:
        if f.classification.family == SourceFileFamily.sensors_log:
            path = Path(f.path)
            if path.exists():
                stat = path.stat()
                key = stable_hash({"session_id": sources.session_id, "owner": sources.owner_node_id,
                                   "source_sha256": f.checksum, "path": str(path),
                                   "size": stat.st_size, "mtime_ns": stat.st_mtime_ns})
                return path, key
    raise HTTPException(
        status_code=404,
        detail="У этой сессии нет доступного sensors.log (файл не найден или уже удалён с диска)",
    )


def _latest_session_id(db: Session) -> str:
    try:
        session_id = SessionReadsRepository(db).latest_id(compute_node_id=get_settings().compute_node_id)
    finally:
        db.rollback()
    if session_id is None:
        raise HTTPException(status_code=404, detail="На этом ПК нет собственной сессии для экспериментального расчёта")
    return session_id


@router.get("/latest")
def get_latest_test_metrics(
    refresh: bool = False, db: Session = Depends(get_db)
) -> dict:
    session_id = _latest_session_id(db)
    return _get_test_metrics(session_id, refresh, db)


@router.get("/{session_id}")
def get_test_metrics(
    session_id: str, refresh: bool = False, db: Session = Depends(get_db)
) -> dict:
    return _get_test_metrics(session_id, refresh, db)


def _get_test_metrics(session_id: str, refresh: bool, db: Session) -> dict:
    sources = read_session_sources(db, session_id)
    if sources is None:
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        require_compute_owner(entity_type="session", entity_id=session_id,
                              origin_compute_node_id=sources.owner_node_id,
                              requested_compute_node_id=get_settings().compute_node_id)
    except ComputeAffinityError as exc:
        raise HTTPException(status_code=403, detail=f"Экспериментальный расчёт выполняется на ПК-владельце. {exc}") from exc
    path, cache_key = _find_sensors_log_path(sources)
    if not refresh:
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

    result = compute_test_metrics(path) | {"session_id": session_id}
    _cache_set(cache_key, result)
    return result
