from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


class NotificationButton(BaseModel):
    text: str
    callback_data: str


class NotificationMessage(BaseModel):
    notification_id: str = Field(default_factory=lambda: f"notification_{uuid4().hex}")
    owner_node_id: str
    channel: str = "telegram"
    text: str
    buttons: list[NotificationButton] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = Field(default_factory=dict)


def build_import_confirmation_message(
    import_job_id: str,
    source_name: str,
    owner_node_id: str,
) -> NotificationMessage:
    return NotificationMessage(
        owner_node_id=owner_node_id,
        text=f"Найдена новая папка логов: {source_name}. Начать импорт?",
        buttons=[
            NotificationButton(text="Импортировать", callback_data=f"import:{import_job_id}:confirm"),
            NotificationButton(text="Игнорировать", callback_data=f"import:{import_job_id}:ignore"),
            NotificationButton(text="Проверить позже", callback_data=f"import:{import_job_id}:postpone"),
        ],
        metadata={"import_job_id": import_job_id, "kind": "import_confirmation"},
    )


def build_copying_retry_message(
    import_job_id: str,
    retry_seconds: int,
    owner_node_id: str,
) -> NotificationMessage:
    return NotificationMessage(
        owner_node_id=owner_node_id,
        text=f"Файлы еще копируются. Повторю проверку через {retry_seconds} секунд.",
        buttons=[
            NotificationButton(text="Проверить сейчас", callback_data=f"import:{import_job_id}:retry"),
            NotificationButton(text="Игнорировать", callback_data=f"import:{import_job_id}:ignore"),
        ],
        metadata={"import_job_id": import_job_id, "kind": "import_still_copying"},
    )


def build_import_summary_message(
    import_job_id: str,
    status: str,
    report_links: list[str],
    missing_context_questions: list[dict[str, Any]],
    owner_node_id: str,
) -> NotificationMessage:
    lines = [f"Импорт логов завершен: {status}."]
    if report_links:
        lines.append("Отчеты:")
        lines.extend(f"- {link}" for link in report_links)
    if missing_context_questions:
        lines.append("Нужно уточнить контекст:")
        lines.extend(f"- {question['question']}" for question in missing_context_questions)
    return NotificationMessage(
        owner_node_id=owner_node_id,
        text="\n".join(lines),
        buttons=[],
        metadata={"import_job_id": import_job_id, "kind": "import_summary"},
    )
