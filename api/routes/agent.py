from fastapi import APIRouter, Depends, HTTPException

from api.deps.repositories import get_runtime_repository
from api.routes.imports import create_detected_import, handle_import_callback
from core.security.auth import require_service_token
from core.security.ratelimit import agent_limiter, rate_limit
from operator_journal.notifications import build_import_confirmation_message, build_import_summary_message
from operator_journal.parser import parse_operator_text
from storage.repositories.runtime import RuntimeRepository


router = APIRouter(prefix="/agent", tags=["agent"], dependencies=[Depends(require_service_token), Depends(rate_limit(agent_limiter))])


@router.get("/active-session")
def active_session(repo: RuntimeRepository = Depends(get_runtime_repository)) -> dict:
    sessions = repo.list_session_payloads()
    # list_session_payloads() is ordered created_at DESC (newest first), so the
    # active/current session is index 0, not -1 (which is the oldest).
    return {"session_id": sessions[0][0] if sessions else None}


@router.post("/operator-event-draft")
def agent_operator_event_draft(payload: dict) -> dict:
    draft = parse_operator_text(payload.get("message", "")).model_dump(mode="json")
    draft["source_channel"] = "openclaw" if payload.get("source") == "openclaw" else "telegram"
    return draft


@router.post("/quality-outcome-draft")
def agent_quality_outcome_draft(payload: dict) -> dict:
    return payload | {"verification_status": "draft", "source_channel": payload.get("source_channel", "openclaw")}


@router.post("/reanalysis-request")
def agent_reanalysis_request(payload: dict) -> dict:
    return {"accepted": True, "reason": payload.get("reason"), "bounded": True}


@router.get("/daily-summary")
def agent_daily_summary(repo: RuntimeRepository = Depends(get_runtime_repository)) -> dict:
    sessions = repo.list_session_payloads()
    return {"sessions": len(sessions), "reports": "available_via_reports_api", "missing_context": []}


@router.post("/notification-ack")
def notification_ack(payload: dict) -> dict:
    return {"acknowledged": True, "payload": payload}


@router.get("/notifications/pending")
def pending_notifications(
    channel: str = "telegram",
    limit: int = 20,
    repo: RuntimeRepository = Depends(get_runtime_repository),
) -> dict:
    return {"notifications": repo.list_pending_notifications(channel=channel, limit=min(limit, 100))}


@router.post("/notifications/{notification_id}/sent")
def notification_sent(
    notification_id: str,
    payload: dict | None = None,
    repo: RuntimeRepository = Depends(get_runtime_repository),
) -> dict:
    ok = repo.mark_notification_sent(notification_id, status_value="sent")
    repo.flush()
    return {"ok": ok, "notification_id": notification_id}


@router.post("/notifications/{notification_id}/failed")
def notification_failed(
    notification_id: str,
    payload: dict | None = None,
    repo: RuntimeRepository = Depends(get_runtime_repository),
) -> dict:
    payload = payload or {}
    ok = repo.mark_notification_sent(
        notification_id,
        status_value="failed",
        error=str(payload.get("error", ""))[:1000] or None,
    )
    repo.flush()
    return {"ok": ok, "notification_id": notification_id}


@router.post("/import-detected")
def agent_import_detected(
    payload: dict,
    repo: RuntimeRepository = Depends(get_runtime_repository),
) -> dict:
    source_path = payload.get("source_path")
    if not source_path:
        raise HTTPException(status_code=400, detail="source_path is required")
    result = create_detected_import(source_path, repo)
    return {
        "job": result.job.model_dump(mode="json"),
        "notifications": [notification.model_dump(mode="json") for notification in result.notifications],
    }


@router.post("/import-callback")
def agent_import_callback(
    payload: dict,
    repo: RuntimeRepository = Depends(get_runtime_repository),
) -> dict:
    callback_data = payload.get("callback_data")
    if not callback_data and payload.get("import_job_id") and payload.get("action"):
        callback_data = f"import:{payload['import_job_id']}:{payload['action']}"
    result = handle_import_callback(callback_data, repo=repo, actor=payload.get("actor", "operator"))
    return {
        "job": result.job.model_dump(mode="json"),
        "notifications": [notification.model_dump(mode="json") for notification in result.notifications],
        "session_ids": result.job.session_ids,
        "report_ids": result.job.report_ids,
    }


@router.post("/imports/{import_job_id}/send-confirmation")
def agent_send_import_confirmation(
    import_job_id: str,
    repo: RuntimeRepository = Depends(get_runtime_repository),
) -> dict:
    job = repo.get_import_job(import_job_id)
    if not job:
        return {"error": "not_found"}
    notification = build_import_confirmation_message(import_job_id, job.source_name)
    repo.save_notifications([notification])
    repo.flush()
    return notification.model_dump(mode="json")


@router.post("/imports/{import_job_id}/send-summary")
def agent_send_import_summary(
    import_job_id: str,
    repo: RuntimeRepository = Depends(get_runtime_repository),
) -> dict:
    job = repo.get_import_job(import_job_id)
    if not job:
        return {"error": "not_found"}
    notification = build_import_summary_message(
        import_job_id,
        job.status.value,
        [f"/reports/{report_id}" for report_id in job.report_ids],
        job.missing_context_questions,
    )
    repo.save_notifications([notification])
    repo.flush()
    return notification.model_dump(mode="json")
