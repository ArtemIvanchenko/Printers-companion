import asyncio
import logging
import signal
import sys
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

from telegram import Update
from telegram.request import HTTPXRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from core.analytics import AnalyticsService
from core.config.settings import get_settings
from core.logging.config import configure_logging
from domain.enums.common import SourceChannel
from operator_journal.parser import parse_operator_text
from operator_journal.telegram_api_client import api_get, api_patch, api_post
from operator_journal.telegram_ui import (
    OPERATOR_PROMPTS,
    main_menu,
    notification_keyboard,
    test_transcript_for,
    voice_confirmation_keyboard,
)
from operator_journal.telegram_voice import (
    build_transcription_audit,
    build_voice_attachment,
    build_voice_operator_event,
)
from operator_journal.voice_transcription import get_voice_transcriber


logger = logging.getLogger(__name__)
CHAT_IDS: set[int] = set()


def is_test_mode(context: ContextTypes.DEFAULT_TYPE) -> bool:
    return bool(context.user_data.get("test_mode"))


def test_mode_prefix(context: ContextTypes.DEFAULT_TYPE) -> str:
    return "ТЕСТОВЫЙ РЕЖИМ\n\n" if is_test_mode(context) else ""


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat:
        CHAT_IDS.add(update.effective_chat.id)
        chat_id = update.effective_chat.id
    else:
        chat_id = "unknown"
    await update.effective_message.reply_text(
        f"{test_mode_prefix(context)}"
        "Бот оператора подключен.\n\n"
        "Здесь можно фиксировать вторичные факторы печати: порошок, газ, обслуживание, ручные паузы, рестарты, "
        "наблюдения и результаты качества. Выберите тип события, затем напишите текст или отправьте голосовое сообщение.\n\n"
        f"chat_id: {chat_id}",
        reply_markup=main_menu(is_test_mode(context)),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "/start — подключить этот чат\n"
        "/menu — показать кнопки оператора\n"
        "/imports — показать последние import jobs\n"
        "/ask <вопрос> — спросить что угодно о данных (LLM понимает смысл)\n"
        "/gas [N] — расход газа за последние N печатей\n"
        "/powder [N] — расход порошка за последние N печатей\n"
        "/last10 [N] — сводка по последним N печатям\n"
        "/defects [N] — статистика брака\n"
        "Кнопка «Тест» включает демонстрационный режим без записи данных.\n"
        "Также можно писать сообщения или отправлять голосовые про порошок, газ, обслуживание и качество."
    )


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        f"{test_mode_prefix(context)}Выберите тип операторского события:",
        reply_markup=main_menu(is_test_mode(context)),
    )


async def imports_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = await api_get("/imports")
    jobs = data if isinstance(data, list) else data.get("imports", [])
    if not jobs:
        await update.effective_message.reply_text("Import jobs пока нет.")
        return
    lines = []
    for job in jobs[:10]:
        lines.append(f"{job['source_name']}: {job['status']} ({job['import_job_id']})")
    await update.effective_message.reply_text("\n".join(lines))


def _parse_n(query_arg: str | None) -> int:
    try:
        n = int(query_arg)
        return max(1, min(n, 100))
    except (ValueError, TypeError):
        return 10


async def ask_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_test_mode(context):
        await update.effective_message.reply_text("ТЕСТОВЫЙ РЕЖИМ\n\nАналитика в тесте не работает.")
        return
    question = " ".join(context.args) if context.args else None
    if not question:
        await update.effective_message.reply_text(
            "Спросите что угодно о данных печати, например:\n"
            "• Какая печать была самой долгой?\n"
            "• Сколько раз меняли фильтр?\n"
            "• Какие дефекты были чаще всего?\n"
            "• Сколько аргона ушло за последние 10 печатей?\n"
            "• Покажи все замены баллонов"
        )
        return
    try:
        svc = AnalyticsService()
        result = svc.answer_question(question)
        svc.close()
        answer = result.get("direct_answer")
        method = result.get("method", "")
        if answer:
            await update.effective_message.reply_text(answer)
        else:
            await update.effective_message.reply_text(
                "Не удалось найти ответ. Попробуйте переформулировать вопрос.\n"
                "Примеры: 'сколько аргона', 'какая самая долгая печать', 'сколько раз меняли фильтр'"
            )
    except Exception as exc:
        logger.exception("Analytics query failed")
        await update.effective_message.reply_text(f"Ошибка при анализе данных: {exc}")


async def gas_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_test_mode(context):
        await update.effective_message.reply_text("ТЕСТОВЫЙ РЕЖИМ\n\nСтатистика газа в тесте не работает.")
        return
    try:
        svc = AnalyticsService()
        data = svc.get_gas_stats(last_n_sessions=_parse_n(context.args[0] if context.args else None))
        svc.close()
        await update.effective_message.reply_text(AnalyticsService._format_gas_answer(data, _parse_n(context.args[0] if context.args else None)))
    except Exception as exc:
        logger.exception("Gas stats failed")
        await update.effective_message.reply_text(f"Ошибка: {exc}")


async def powder_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_test_mode(context):
        await update.effective_message.reply_text("ТЕСТОВЫЙ РЕЖИМ\n\nСтатистика порошка в тесте не работает.")
        return
    try:
        n = _parse_n(context.args[0] if context.args else None)
        svc = AnalyticsService()
        data = svc.get_powder_stats(last_n_sessions=n)
        svc.close()
        await update.effective_message.reply_text(AnalyticsService._format_powder_answer(data, n))
    except Exception as exc:
        logger.exception("Powder stats failed")
        await update.effective_message.reply_text(f"Ошибка: {exc}")


async def last10_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_test_mode(context):
        await update.effective_message.reply_text("ТЕСТОВЫЙ РЕЖИМ\n\nСписок печатей в тесте не работает.")
        return
    try:
        n = _parse_n(context.args[0] if context.args else None)
        svc = AnalyticsService()
        sessions = svc.get_session_summary(last_n=n)
        svc.close()
        await update.effective_message.reply_text(AnalyticsService._format_sessions_answer(sessions))
    except Exception as exc:
        logger.exception("Session summary failed")
        await update.effective_message.reply_text(f"Ошибка: {exc}")


async def defects_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_test_mode(context):
        await update.effective_message.reply_text("ТЕСТОВЫЙ РЕЖИМ\n\nСтатистика дефектов в тесте не работает.")
        return
    try:
        n = _parse_n(context.args[0] if context.args else None)
        svc = AnalyticsService()
        data = svc.get_print_quality_stats(last_n_sessions=n)
        svc.close()
        await update.effective_message.reply_text(AnalyticsService._format_quality_answer(data, n))
    except Exception as exc:
        logger.exception("Quality stats failed")
        await update.effective_message.reply_text(f"Ошибка: {exc}")


async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_test_mode(context):
        await update.effective_message.reply_text("ТЕСТОВЫЙ РЕЖИМ\n\nОдобрение в тесте не работает.")
        return
    if not context.args:
        await update.effective_message.reply_text(
            "Используйте: /approve <session_id>\n"
            "Например: /approve sess_001\n\n"
            "Система запомнит параметры этой печати как норму."
        )
        return
    session_id = context.args[0]
    await _handle_session_approval(update, session_id)


async def _handle_session_approval(update, session_id: str) -> None:
    try:
        from operator_journal.telegram_api_client import api_post
        await api_post(
            f"/sessions/{session_id}/approve",
            {"confirmed_by": f"telegram:{update.effective_user.id if update.effective_user else 'unknown'}"},
        )
        text = (
            f"✅ Сессия {session_id} принята.\n\n"
            "Система запомнила параметры этой печати как норму. "
            "В будущем аналогичные отклонения не будут считаться проблемами."
        )
        if hasattr(update, 'effective_message'):
            await update.effective_message.reply_text(text)
        else:
            await update.edit_message_text(text)
    except Exception as exc:
        logger.exception("Session approval failed")
        text = f"Не удалось принять сессию: {exc}"
        if hasattr(update, 'effective_message'):
            await update.effective_message.reply_text(text)
        else:
            await update.edit_message_text(text)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()

    # Handle session approval
    if query.data and query.data.startswith("approve:"):
        session_id = query.data.split(":", 1)[1]
        await _handle_session_approval_for_callback(query, context, session_id)
        return

    if query.data and query.data.startswith("test:"):
        action = query.data.split(":", 1)[1]
        if action == "enter":
            context.user_data["test_mode"] = True
            context.user_data.pop("operator_entry_kind", None)
            context.user_data.pop("pending_voice_event_id", None)
            context.user_data.pop("pending_voice_submissions", None)
            await query.edit_message_text(
                "ТЕСТОВЫЙ РЕЖИМ включён.\n\n"
                "Можно нажимать кнопки, писать текст и отправлять голосовые. "
                "Бот покажет, что было бы создано, но ничего не запишет в журнал/API и не отправит в модели.",
                reply_markup=main_menu(True),
            )
            return
        context.user_data.clear()
        await query.edit_message_text(
            "Тестовый режим выключен. Бот снова работает в обычном режиме.",
            reply_markup=main_menu(False),
        )
        return

    if query.data and query.data.startswith("voice_"):
        await handle_voice_confirmation_callback(query, context)
        return

    if query.data and query.data.startswith("op:"):
        action = query.data.split(":", 1)[1]
        if action == "imports":
            if is_test_mode(context):
                await query.edit_message_text(
                    "ТЕСТОВЫЙ РЕЖИМ\n\nЗдесь был бы список последних импортов. В тесте API не вызывается.",
                    reply_markup=main_menu(True),
                )
                return
            data = await api_get("/imports")
            jobs = data if isinstance(data, list) else data.get("imports", [])
            if not jobs:
                await query.edit_message_text(
                    "Import jobs пока нет.",
                    reply_markup=main_menu(is_test_mode(context)),
                )
                return
            lines = [f"{job['source_name']}: {job['status']}" for job in jobs[:10]]
            await query.edit_message_text("\n".join(lines), reply_markup=main_menu(is_test_mode(context)))
            return
        context.user_data["operator_entry_kind"] = action
        await query.edit_message_text(
            f"{test_mode_prefix(context)}{OPERATOR_PROMPTS.get(action, OPERATOR_PROMPTS['note'])}",
            reply_markup=main_menu(is_test_mode(context)) if is_test_mode(context) else None,
        )
        return

    try:
        result = await api_post(
            "/agent/import-callback",
            {"callback_data": query.data, "actor": f"telegram:{query.from_user.id}"},
        )
        job = result["job"]
        await query.edit_message_text(f"{job['source_name']}: {job['status']}")
    except Exception as exc:
        logger.exception("Telegram callback failed")
        await query.edit_message_text(f"Не удалось обработать кнопку: {exc}")


async def _handle_session_approval_for_callback(query, context: ContextTypes.DEFAULT_TYPE, session_id: str) -> None:
    try:
        from operator_journal.telegram_api_client import api_post
        await api_post(
            f"/sessions/{session_id}/approve",
            {"confirmed_by": f"telegram:{query.from_user.id}"},
        )
        await query.edit_message_text(
            f"✅ Сессия {session_id} принята.\n\n"
            "Система запомнила параметры этой печати как норму. "
            "В будущем аналогичные отклонения не будут считаться проблемами.",
            reply_markup=main_menu(is_test_mode(context)),
        )
    except Exception as exc:
        logger.exception("Session approval failed")
        await query.edit_message_text(f"Не удалось принять сессию: {exc}")


async def handle_voice_confirmation_callback(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    action, pending_key = query.data.split(":", 1)
    pending = context.user_data.get("pending_voice_submissions", {}).get(pending_key)
    if not pending:
        await query.edit_message_text(
            "Эта расшифровка уже обработана или устарела.",
            reply_markup=main_menu(is_test_mode(context)),
        )
        return

    if action == "voice_rerecord":
        context.user_data["pending_voice_submissions"].pop(pending_key, None)
        if pending.get("test_mode"):
            await query.edit_message_text(
                "ТЕСТОВЫЙ РЕЖИМ\n\n"
                "Расшифровка отклонена. В обычном режиме бот попросил бы записать голосовое ещё раз.",
                reply_markup=main_menu(True),
            )
            return
        await api_patch(
            f"/operator-journal/{pending['journal_entry_id']}",
            {"status": "rerecord_requested"},
        )
        await query.edit_message_text(
            "Хорошо, не отправляю эту расшифровку. Запишите голосовое сообщение ещё раз.",
            reply_markup=main_menu(is_test_mode(context)),
        )
        return

    try:
        if pending.get("test_mode"):
            text = pending["transcript"]
            draft = parse_operator_text(text, source_channel=SourceChannel.telegram).model_dump(mode="json")
            context.user_data["pending_voice_submissions"].pop(pending_key, None)
            await query.edit_message_text(
                "ТЕСТОВЫЙ РЕЖИМ\n\n"
                "Кнопка «Отправить» нажата. В обычном режиме сейчас создалось бы операторское событие:\n"
                f"type: {draft.get('event_type')}\n"
                f"confidence: {draft.get('confidence')}\n"
                f"status: {draft.get('verification_status')}\n\n"
                "В тесте ничего не записано и никуда не отправлено.",
                reply_markup=main_menu(True),
            )
            return

        payload = pending["operator_event_payload"]
        payload.setdefault("audit_trail", []).append(
            {"action": "operator_confirmed_voice_transcript", "journal_entry_id": pending["journal_entry_id"]}
        )
        created = await api_post("/operator-events", payload)
        await api_patch(
            f"/operator-journal/{pending['journal_entry_id']}",
            {
                "status": "submitted",
                "operator_event_id": created["event_id"],
                "normalized_text": pending["transcript"],
            },
        )
        context.user_data["pending_voice_submissions"].pop(pending_key, None)
        await query.edit_message_text(
            "Расшифровка отправлена в журнал и обработана как операторское событие:\n"
            f"type: {created.get('event_type')}\n"
            f"confidence: {created.get('confidence')}\n"
            f"status: {created.get('verification_status')}",
            reply_markup=main_menu(is_test_mode(context)),
        )
    except Exception as exc:
        logger.exception("Voice confirmation failed")
        await query.edit_message_text(
            f"Не удалось отправить расшифровку: {exc}",
            reply_markup=main_menu(is_test_mode(context)),
        )


async def handle_operator_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not message.text:
        return
    if update.effective_chat:
        CHAT_IDS.add(update.effective_chat.id)
    try:
        if is_test_mode(context):
            entry_kind = context.user_data.pop("operator_entry_kind", None)
            text = f"[{entry_kind}] {message.text}" if entry_kind else message.text
            draft = parse_operator_text(text, source_channel=SourceChannel.telegram).model_dump(mode="json")
            await message.reply_text(
                "ТЕСТОВЫЙ РЕЖИМ\n\n"
                "Текст разобран локально. В обычном режиме была бы создана запись журнала и операторское событие:\n"
                f"type: {draft.get('event_type')}\n"
                f"confidence: {draft.get('confidence')}\n"
                f"status: {draft.get('verification_status')}\n\n"
                "В тесте ничего не записано и никуда не отправлено.",
                reply_markup=main_menu(True),
            )
            return

        pending_voice_event_id = context.user_data.pop("pending_voice_event_id", None)
        if pending_voice_event_id:
            updated = await api_patch(
                f"/operator-events/{pending_voice_event_id}",
                {
                    "note": message.text,
                    "confidence": 0.55,
                    "verification_status": "unverified",
                    "parse_warnings": ["Voice note was transcribed or clarified by operator text."],
                },
            )
            await message.reply_text(
                "Текст добавлен к голосовому операторскому событию:\n"
                f"type: {updated.get('event_type')}\n"
                f"status: {updated.get('verification_status')}",
                reply_markup=main_menu(is_test_mode(context)),
            )
            return

        entry_kind = context.user_data.pop("operator_entry_kind", None)
        text = message.text
        if entry_kind:
            text = f"[{entry_kind}] {text}"
        draft = await api_post(
            "/agent/operator-event-draft",
            {"message": text, "source": "telegram"},
        )
        draft["created_by"] = f"telegram:{update.effective_user.id if update.effective_user else 'unknown'}"
        draft["source_channel"] = "telegram"
        created = await api_post("/operator-events", draft)
        await api_post(
            "/operator-journal",
            {
                "source_channel": "telegram",
                "created_by": draft["created_by"],
                "entry_kind": "operator_text",
                "raw_text": message.text,
                "normalized_text": text,
                "operator_event_id": created["event_id"],
                "status": "submitted",
                "audit_trail": [{"action": "text_received_and_parsed"}],
            },
        )
        await message.reply_text(
            "Создан черновик операторского события:\n"
            f"type: {created.get('event_type')}\n"
            f"confidence: {created.get('confidence')}\n"
            f"status: {created.get('verification_status')}",
            reply_markup=main_menu(is_test_mode(context)),
        )
    except Exception as exc:
        logger.exception("Operator text handling failed")
        await message.reply_text(f"Не удалось сохранить событие: {exc}")


async def handle_operator_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not message.voice:
        return
    if update.effective_chat:
        CHAT_IDS.add(update.effective_chat.id)
    try:
        voice = message.voice
        entry_kind = context.user_data.pop("operator_entry_kind", None)
        if is_test_mode(context):
            transcript = test_transcript_for(entry_kind)
            pending_key = uuid4().hex
            context.user_data.setdefault("pending_voice_submissions", {})[pending_key] = {
                "journal_entry_id": f"test_journal_{pending_key}",
                "operator_event_payload": {},
                "transcript": transcript,
                "test_mode": True,
            }
            await message.reply_text(
                "ТЕСТОВЫЙ РЕЖИМ\n\n"
                "Голосовое получено. В тесте бот не скачивает аудио и не отправляет его в STT-модель.\n\n"
                "Демонстрационная расшифровка:\n\n"
                f"{transcript}\n\n"
                "Проверьте сценарий подтверждения:",
                reply_markup=voice_confirmation_keyboard(pending_key),
            )
            return

        voice_metadata = {
            "file_id": voice.file_id,
            "file_unique_id": voice.file_unique_id,
            "duration": voice.duration,
            "mime_type": voice.mime_type,
            "file_size": voice.file_size,
        }
        created_by = f"telegram:{update.effective_user.id if update.effective_user else 'unknown'}"
        audio_path = await _download_voice_to_temp(context, voice.file_id, voice.file_unique_id)
        transcriber = context.application.bot_data["voice_transcriber"]
        transcription = await asyncio.to_thread(transcriber.transcribe, audio_path)
        _remove_temp_file(audio_path)

        if transcription.success and transcription.text:
            text = transcription.text
            draft_text = f"[{entry_kind}] {text}" if entry_kind else text
            payload = await api_post(
                "/agent/operator-event-draft",
                {"message": draft_text, "source": "telegram"},
            )
            payload["created_by"] = created_by
            payload["source_channel"] = "telegram"
            payload["note"] = text
            payload["attachments"] = [build_voice_attachment(voice_metadata)]
            payload.setdefault("audit_trail", []).append(
                build_transcription_audit(transcription.__dict__)
            )
            pending_key = uuid4().hex
            journal = await api_post(
                "/operator-journal",
                {
                    "source_channel": "telegram",
                    "created_by": created_by,
                    "entry_kind": "operator_voice",
                    "raw_text": text,
                    "normalized_text": draft_text,
                    "voice_attachment": build_voice_attachment(voice_metadata),
                    "transcription": transcription.__dict__,
                    "status": "awaiting_operator_confirmation",
                    "audit_trail": [{"action": "voice_transcribed_awaiting_operator_confirmation"}],
                },
            )
            context.user_data.setdefault("pending_voice_submissions", {})[pending_key] = {
                "journal_entry_id": journal["journal_entry_id"],
                "operator_event_payload": payload,
                "transcript": text,
            }
            await message.reply_text(
                "Проверьте расшифровку голосового сообщения:\n\n"
                f"{text}\n\n"
                "Если всё верно, нажмите «Отправить». Если текст распознан неправильно, нажмите «Перезаписать».",
                reply_markup=voice_confirmation_keyboard(pending_key),
            )
            return
        else:
            payload = build_voice_operator_event(
                voice_metadata=voice_metadata,
                created_by=created_by,
                entry_kind=entry_kind,
            )
            payload.setdefault("audit_trail", []).append(
                build_transcription_audit(transcription.__dict__)
            )
        created = await api_post("/operator-events", payload)
        await api_post(
            "/operator-journal",
            {
                "source_channel": "telegram",
                "created_by": created_by,
                "entry_kind": "operator_voice",
                "voice_attachment": build_voice_attachment(voice_metadata),
                "transcription": transcription.__dict__,
                "operator_event_id": created["event_id"],
                "status": "needs_manual_transcription",
                "audit_trail": [{"action": "voice_transcription_failed"}],
            },
        )
        context.user_data["pending_voice_event_id"] = created["event_id"]
        await message.reply_text(
            "Голосовое сообщение сохранено, но автоматическая расшифровка не удалась.\n"
            "Отправьте следующим сообщением короткую текстовую расшифровку.",
            reply_markup=main_menu(is_test_mode(context)),
        )
    except Exception as exc:
        logger.exception("Operator voice handling failed")
        await message.reply_text(f"Не удалось сохранить голосовое событие: {exc}")


async def _download_voice_to_temp(
    context: ContextTypes.DEFAULT_TYPE,
    file_id: str,
    file_unique_id: str | None,
) -> Path:
    temp_dir = Path("/tmp/telegram_voice")
    temp_dir.mkdir(parents=True, exist_ok=True)
    suffix = file_unique_id or uuid4().hex
    audio_path = temp_dir / f"{suffix}.oga"
    telegram_file = await context.bot.get_file(file_id)
    await telegram_file.download_to_drive(custom_path=audio_path)
    return audio_path


def _remove_temp_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("Could not remove temporary voice file: %s", path)


async def poll_notifications(app: Application) -> None:
    """Poll and send pending notifications with timeout and error recovery."""
    backoff_delay = 5.0
    max_backoff = 60.0
    
    while True:
        try:
            if CHAT_IDS:
                try:
                    data = await asyncio.wait_for(
                        api_get("/agent/notifications/pending?channel=telegram&limit=20"),
                        timeout=30.0
                    )
                    for notification in data.get("notifications", []):
                        sent = False
                        for chat_id in list(CHAT_IDS):
                            try:
                                await asyncio.wait_for(
                                    app.bot.send_message(
                                        chat_id=chat_id,
                                        text=notification["text"],
                                        reply_markup=notification_keyboard(notification.get("buttons", [])),
                                        disable_web_page_preview=True,
                                    ),
                                    timeout=30.0
                                )
                                sent = True
                            except asyncio.TimeoutError:
                                logger.error("Timeout sending notification to chat %s", chat_id)
                            except Exception as exc:
                                logger.warning("Failed to send notification to chat %s: %s", chat_id, exc)
                        
                        if sent:
                            try:
                                await asyncio.wait_for(
                                    api_post(f"/agent/notifications/{notification['notification_id']}/sent"),
                                    timeout=30.0
                                )
                            except Exception as exc:
                                logger.warning("Failed to mark notification as sent: %s", exc)
                    
                    # Reset backoff on success
                    backoff_delay = 5.0
                except asyncio.TimeoutError:
                    logger.error("Timeout fetching notifications from API")
                    backoff_delay = min(backoff_delay * 1.5, max_backoff)
            
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            logger.info("Notification poll task cancelled")
            raise
        except Exception as exc:
            logger.error("Notification polling failed with exception: %s", exc)
            await asyncio.sleep(backoff_delay)
            backoff_delay = min(backoff_delay * 1.5, max_backoff)


async def post_init(app: Application) -> None:
    app.bot_data["voice_transcriber"] = get_voice_transcriber(get_settings())
    default_chat_id = get_settings().telegram_default_chat_id.strip()
    if default_chat_id:
        CHAT_IDS.add(int(default_chat_id))
    app.bot_data["notification_poll_task"] = asyncio.create_task(poll_notifications(app))


async def post_shutdown(app: Application) -> None:
    task = app.bot_data.get("notification_poll_task")
    if task:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    
    if not settings.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is required for telegram-bot profile")
    if not settings.agent_api_token:
        raise SystemExit("AGENT_API_TOKEN is required for telegram-bot profile")

    request = HTTPXRequest(
        connect_timeout=30,
        read_timeout=60,
        write_timeout=60,
        pool_timeout=30,
        proxy=settings.telegram_proxy_url or None,
    )
    app = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .request(request)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CommandHandler("imports", imports_command))
    app.add_handler(CommandHandler("ask", ask_command))
    app.add_handler(CommandHandler("gas", gas_command))
    app.add_handler(CommandHandler("powder", powder_command))
    app.add_handler(CommandHandler("last10", last10_command))
    app.add_handler(CommandHandler("defects", defects_command))
    app.add_handler(CommandHandler("approve", approve_command))
    app.add_handler(CallbackQueryHandler(handle_callback, pattern=r"^(import|op|test|voice_send|voice_rerecord|approve):"))
    app.add_handler(MessageHandler(filters.VOICE, handle_operator_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_operator_text))
    
    logger.info("Telegram bot started in polling mode")
    
    # Handle graceful shutdown
    stop_event = asyncio.Event()
    
    def signal_handler(signum: int, frame: object) -> None:
        logger.info("Received signal %s, stopping bot gracefully", signum)
        stop_event.set()
    
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    try:
        app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
    except KeyboardInterrupt:
        logger.info("Bot interrupted by user")
    except Exception as exc:
        logger.exception("Bot crashed with exception: %s", exc)
        sys.exit(1)
    finally:
        logger.info("Bot shutdown complete")


if __name__ == "__main__":
    main()
