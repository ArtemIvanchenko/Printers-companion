from telegram import InlineKeyboardButton, InlineKeyboardMarkup


OPERATOR_PROMPTS = {
    "powder": (
        "Материал / порошок\n\n"
        "Напишите одной фразой, например:\n"
        "Порошок AlSi10Mg партия AL-2026-04-12, первый цикл\n"
        "Порошок 316L batch S-17 reuse cycle 3, просеян"
    ),
    "gas": (
        "Газ\n\n"
        "Напишите одной фразой, например:\n"
        "Поставили новый баллон аргона AG-042, давление 180 бар\n"
        "Газ аргон, баллон AG-042"
    ),
    "maintenance": (
        "Обслуживание\n\n"
        "Напишите, что сделали, например:\n"
        "Поменяли уплотнитель двери камеры\n"
        "Почистили оптику\n"
        "Заменили фильтр"
    ),
    "operation": (
        "Операция / ручное вмешательство\n\n"
        "Напишите событие, например:\n"
        "После слоя 6843 был ручной рестарт\n"
        "Ручная пауза из-за проверки камеры"
    ),
    "quality": (
        "Качество\n\n"
        "Напишите результат, например:\n"
        "Печать 27.03 принята, видимых дефектов нет\n"
        "Деталь забракована: поры после резки, зона примерно середина высоты"
    ),
    "note": (
        "Наблюдение оператора\n\n"
        "Напишите свободно, например:\n"
        "После продувки был необычный запах\n"
        "Во время слоя 1200 слышен нестандартный звук"
    ),
}


def notification_keyboard(buttons: list[dict[str, str]]) -> InlineKeyboardMarkup | None:
    if not buttons:
        return None
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(button["text"], callback_data=button["callback_data"])] for button in buttons]
    )


def voice_confirmation_keyboard(pending_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Отправить", callback_data=f"voice_send:{pending_key}"),
                InlineKeyboardButton("Перезаписать", callback_data=f"voice_rerecord:{pending_key}"),
            ]
        ]
    )


def main_menu(test_mode: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("Порошок / материал", callback_data="op:powder"),
            InlineKeyboardButton("Газ", callback_data="op:gas"),
        ],
        [
            InlineKeyboardButton("Обслуживание", callback_data="op:maintenance"),
            InlineKeyboardButton("Пауза / рестарт", callback_data="op:operation"),
        ],
        [
            InlineKeyboardButton("Качество", callback_data="op:quality"),
            InlineKeyboardButton("Наблюдение", callback_data="op:note"),
        ],
        [InlineKeyboardButton("Последние импорты", callback_data="op:imports")],
    ]
    if test_mode:
        rows.append([InlineKeyboardButton("Выйти из теста", callback_data="test:exit")])
    else:
        rows.append([InlineKeyboardButton("Тест", callback_data="test:enter")])
    return InlineKeyboardMarkup(rows)


def test_transcript_for(entry_kind: str | None) -> str:
    examples = {
        "powder": "Порошок AlSi10Mg партия AL-2026-04-12, первый цикл",
        "gas": "Поставили новый баллон аргона AG-042, давление 180 бар",
        "maintenance": "Почистили оптику и заменили фильтр",
        "operation": "После слоя 6843 был ручной рестарт",
        "quality": "Печать принята, видимых дефектов нет",
        "note": "Во время продувки был необычный запах",
    }
    return examples.get(entry_kind or "note", examples["note"])


def session_approval_keyboard(session_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Печать ОК (запомнить)", callback_data=f"approve:{session_id}")],
            [InlineKeyboardButton("❌ Есть проблемы", callback_data="op:quality")],
        ]
    )
