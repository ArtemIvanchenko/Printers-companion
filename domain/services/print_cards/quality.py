"""Quality-control use cases for a print card, independent of HTTP.

Pass a clean session: reads release their transaction before returning data or
building a report; publication owns the commit of the label, job and revision.
Callers must perform cache invalidation only after successful publication.
"""

from copy import deepcopy

from pydantic import ValidationError

from domain.services.print_cards.contracts import CardError
from domain.services.quality import create_final_print_outcome
from storage.repositories.prints_repo import PrintsRepository
from storage.repositories.runtime import QualityOutcomeConflict, RuntimeRepository


def list_quality_outcomes(repo: PrintsRepository, record_id: str) -> list[dict]:
    """Read the card's append-only inspection history without retaining SQL."""
    try:
        if not repo.get_print_record(record_id):
            raise CardError("not_found", "Карточка печати не найдена")
        return deepcopy(RuntimeRepository(repo.db).list_quality_outcomes(print_record_id=record_id))
    finally:
        repo.db.rollback()


def create_quality_outcome(
    repo: PrintsRepository, record_id: str, payload: dict, *, actor: str
) -> dict:
    """Atomically append a final label and enqueue owner-local retraining.

    Lock and refresh the card before choosing the session: a link changed by
    another request must not survive in this session's ORM identity map. The
    repository also checks that the correction extends the current audit head.
    No model training or report generation runs inside this transaction.
    """
    from analytics.prediction.retraining import enqueue_retraining

    try:
        record = repo.get_print_record_for_update(record_id)
        if not record:
            raise CardError("not_found", "Карточка печати не найдена")
        try:
            draft = create_final_print_outcome(
                payload,
                print_record_id=record_id,
                session_id=record.get("session_id"),
                created_by=actor,
            )
        except ValidationError as exc:
            raise CardError("invalid_inputs", str(exc)) from exc

        outcome = draft.model_dump(mode="json")
        try:
            RuntimeRepository(repo.db).save_quality_outcome(outcome)
        except QualityOutcomeConflict as exc:
            raise CardError("conflict", str(exc)) from exc
        except ValueError as exc:
            raise CardError("invalid_inputs", str(exc)) from exc

        retraining_job = None
        if outcome.get("session_id"):
            retraining_job = enqueue_retraining(
                repo.db,
                session_id=str(outcome["session_id"]),
                outcome_id=str(outcome["outcome_id"]),
                result=str(outcome["result"]),
                timestamp=str(outcome["timestamp"]),
            )
        # The dependent row and its notification through the card event stream
        # become visible together, as does the durable request to retrain.
        repo._touch_print_record(record_id)
        repo.db.commit()
    except Exception:
        repo.db.rollback()
        raise
    return {**outcome, "model_retraining_job_id": (retraining_job or {}).get("job_id")}


def get_operator_report(repo: PrintsRepository, record_id: str) -> dict:
    """Copy report inputs, release NAS SQL, then build the report locally."""
    from domain.services.operator_report import build_operator_report

    try:
        record = repo.get_print_record(record_id)
        if not record:
            raise CardError("not_found", "Карточка печати не найдена")
        runtime = RuntimeRepository(repo.db)
        session_id = record.get("session_id")
        payload = runtime.get_session_payload(session_id) if session_id else None
        inputs = deepcopy(
            {
                "session_id": session_id,
                "group": (payload or {}).get("group") or {},
                "quality_outcomes": runtime.list_quality_outcomes(print_record_id=record_id),
                "print_record": record,
            }
        )
    finally:
        repo.db.rollback()
    return build_operator_report(**inputs)
