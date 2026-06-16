from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from api.deps.repositories import get_runtime_repository
from operator_journal.journal_entries import build_operator_journal_entry, build_export_payload
from storage.repositories.runtime import RuntimeRepository


router = APIRouter(prefix="/operator-journal", tags=["operator-journal"])


@router.post("")
def create_operator_journal_entry(
    payload: dict,
    repo: RuntimeRepository = Depends(get_runtime_repository),
) -> dict:
    entry = payload
    if "journal_entry_id" not in entry:
        entry = build_operator_journal_entry(
            source_channel=payload.get("source_channel", "telegram"),
            created_by=payload.get("created_by", "unknown"),
            entry_kind=payload.get("entry_kind", "operator_input"),
            raw_text=payload.get("raw_text"),
            normalized_text=payload.get("normalized_text"),
            voice_attachment=payload.get("voice_attachment"),
            transcription=payload.get("transcription"),
            operator_event_id=payload.get("operator_event_id"),
            status=payload.get("status", "draft"),
            project_id=payload.get("project_id"),
            platform_id=payload.get("platform_id"),
            duplicate_targets=payload.get("duplicate_targets"),
            audit_trail=payload.get("audit_trail"),
        )
    saved = repo.save_operator_journal_entry(entry)
    repo.flush()
    return saved


@router.get("")
def list_operator_journal_entries(repo: RuntimeRepository = Depends(get_runtime_repository)) -> list[dict]:
    return repo.list_operator_journal_entries()


@router.get("/{journal_entry_id}")
def get_operator_journal_entry(
    journal_entry_id: str,
    repo: RuntimeRepository = Depends(get_runtime_repository),
) -> dict:
    entry = repo.get_operator_journal_entry(journal_entry_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Operator journal entry not found")
    return entry


@router.patch("/{journal_entry_id}")
def update_operator_journal_entry(
    journal_entry_id: str,
    payload: dict,
    repo: RuntimeRepository = Depends(get_runtime_repository),
) -> dict:
    entry = repo.get_operator_journal_entry(journal_entry_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Operator journal entry not found")
    before = entry.copy()
    entry.update(payload)
    entry["updated_at"] = datetime.now(timezone.utc).isoformat()
    entry["export_payload"] = build_export_payload(entry)
    entry.setdefault("audit_trail", []).append(
        {
            "action": "patch",
            "changed_fields": sorted(payload.keys()),
            "before_status": before.get("status"),
            "after_status": entry.get("status"),
            "at": entry["updated_at"],
        }
    )
    saved = repo.save_operator_journal_entry(entry)
    repo.flush()
    return saved
