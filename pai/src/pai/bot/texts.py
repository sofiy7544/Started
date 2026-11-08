"""Тексты интерфейса."""

from __future__ import annotations

from decimal import Decimal

BTN_NEW = "🆕 Новый диалог"
BTN_MODEL = "🤖 Модель"
BTN_STATS = "📊 Статистика"
BTN_SETTINGS = "⚙️ Настройки"
BTN_HELP = "❓ Помощь"
BTN_MEMORY = "🧠 Память"
BTN_VOICE = "🎙 Голос"
MENU_BUTTONS = (BTN_NEW, BTN_MEMORY, BTN_MODEL, BTN_VOICE, BTN_SETTINGS, BTN_STATS, BTN_HELP)

STYLE_TITLES = {"short": "коротко", "normal": "обычно", "detailed": "подробно"}


def money(value: Decimal) -> str:
    if value == 0:
        return "$0"
    if value < Decimal("0.01"):
        return f"${value:.4f}"
    return f"${value:.2f}"


def welcome(first_name: str | None) -> str:
    hello = f"Привет, {first_name}!" if first_name else "Привет!"
    return (
        f"{hello} Я ваш личный AI-ассистент.\n\n"
        "Просто пишите мне как человеку — вопросы, задачи, черновики, идеи. "
        "Я помню текущий разговор; кнопка «🆕 Новый диалог» начинает тему с чистого листа.\n\n"
        "Меню внизу: модель, настройки и расходы. Подробнее — «❓ Помощь»."
    )


HELP = (
    "Что я умею:\n"
    "• отвечать на вопросы и помогать с текстами, планами, идеями;\n"
    "• понимать голосовые (русский и украинский) — можно наговорить длинную задачу — и отвечать голосом — «🎙 Голос»;\n"
    "• работать с фото и скриншотами: прочитать текст, найти ошибку, разобрать чек, таблицу, график, интерфейс;\n"
    "• читать документы: PDF (и сканы), Word, Excel, PowerPoint, CSV, TXT, JSON, XML, ZIP — сравнить, найти, "
    "извлечь данные, сделать отчёт или таблицу файлом;\n"
    "• разбирать видео и аудио: что происходит, кто что говорит, расшифровка, краткое содержание, нужный момент;\n"
    "• понимать, что фото, голосовое и текст подряд — одна задача: «а теперь сравни с предыдущим»;\n"
    "• помнить разговор и важное о вас — «🧠 Память»;\n"
    "• вести список задач: диктуйте план, «добавь задачу», «сделал …» — «📋 Задачи», выгрузка в Excel;\n"
    "• напоминать: «напомни завтра в 10 позвонить врачу» — «⏰ Напоминания»;\n"
    "• иногда писать первым по вашим делам — /proactive_on;\n"
    "• работать на разных моделях — «🤖 Модель»; расходы — «📊 Статистика».\n\n"
    "Файлы — до 20 МБ (ограничение Telegram для ботов). Фото без сжатия — отправьте «как файл».\n"
    "Файлы хранятся сутки, извлечённый текст — вместе с разговором.\n\n"
    "Команды: /new, /tasks, /memory, /remember, /reminders, /add_reminder, /delete_reminder, /proactive_on, "
    "/proactive_off, /timezone, /model, /voice, /stats, /settings, /menu."
)

ACCESS_DENIED = "🔒 Это личный бот. Доступ закрыт."
NEW_CONVERSATION = "🆕 Начали новый диалог. Предыдущий разговор сохранён в истории."
TOO_FAST = "Слишком много сообщений подряд — подождите минуту."
BUDGET_EXHAUSTED = "💸 Месячный лимит расходов исчерпан. Его можно увеличить в .env (MONTHLY_BUDGET_USD)."
PAID_DISABLED = "⏸ Платные модели отключены (PAID_APIS_DISABLED=1). Можно выбрать локальную модель."
EMPTY_ANSWER = "Модель вернула пустой ответ. Попробуйте переформулировать вопрос."
INTERNAL_ERROR = "⚠️ Что-то пошло не так. Ошибка записана в лог, попробуйте ещё раз."

ERR_AUTH = "🔑 Провайдер отклонил API-ключ. Проверьте ключ в .env или выберите другую модель."
ERR_QUOTA = "💳 У провайдера закончился баланс или квота. Пополните счёт или выберите другую модель."
ERR_RATE_LIMIT = "⏳ Провайдер просит не так часто. Повторите через минуту."
ERR_TEMPORARY = "📡 Сервис модели временно недоступен. Я уже повторил попытку — напишите ещё раз через минуту."
ERR_TIMEOUT = "⌛ Модель не ответила вовремя. Попробуйте ещё раз или выберите более быструю модель."
ERR_MODEL_NOT_FOUND = "🤖 Эта модель недоступна для вашего ключа. Выберите другую: «🤖 Модель»."
ERR_CONTEXT_TOO_LONG = "📚 Разговор стал слишком длинным для модели. Нажмите «🆕 Новый диалог»."
ERR_TOO_MANY_STEPS = "Задача потребовала слишком много шагов и была остановлена, чтобы не тратить деньги."
ERR_BAD_RESPONSE = "Модель ответила некорректно. Попробуйте ещё раз или переформулируйте."
ERR_REFUSED = "Модель отказалась отвечать на этот запрос (фильтр провайдера)."
ERR_CONFIG = "⚙️ Ошибка настройки модели. Подробности — в логе бота."
ERR_PAUSED = "⏸ После нескольких ошибок подряд я сделал паузу в обращениях к модели. Попробуйте через минуту."

NOT_YET_FILES = "📎 Обработка файлов выключена (MEDIA_ENABLED=0 в .env). Пока опишите задачу текстом."
NOT_YET_OTHER = "Пока я понимаю только текстовые сообщения."
UNKNOWN_COMMAND = "Не знаю такой команды. Список — «❓ Помощь»."

MODEL_HEADER = "Выберите модель. Цена — за 1 млн токенов (примерно 400–700 тыс. символов текста)."
INSTRUCTIONS_PROMPT = (
    "Напишите одним сообщением, как мне с вами работать. Например:\n"
    "«Обращайся на ты. Я маркетолог, веду проекты для e-commerce. Отвечай без воды.»\n\n"
    "Отмена — /cancel."
)
INSTRUCTIONS_SAVED = "✅ Инструкции сохранены — буду их учитывать."
INSTRUCTIONS_CLEARED = "🗑 Личные инструкции удалены."
CANCELLED = "Отменено."


def model_set(title: str) -> str:
    return f"✅ Теперь отвечаю моделью {title}."


def settings_text(*, model_title: str, style: str, instructions: str | None, budget: Decimal,
                  spent: Decimal) -> str:
    instr = f"«{instructions[:300]}{'…' if len(instructions) > 300 else ''}»" if instructions else "нет"
    return (
        "⚙️ Настройки\n\n"
        f"Модель: {model_title}\n"
        f"Стиль ответов: {STYLE_TITLES.get(style, style)}\n"
        f"Личные инструкции: {instr}\n"
        f"Бюджет в месяц: {money(budget)} (потрачено {money(spent)})\n\n"
        "Бюджет и ключи меняются в файле .env."
    )


def stats_text(stats, *, budget: Decimal, days_left: int) -> str:  # type: ignore[no-untyped-def]
    lines = [
        "📊 Статистика",
        "",
        f"Этот месяц: {money(stats.month_cost)} из {money(budget)} "
        f"({stats.month_requests} запросов), осталось дней: {days_left}",
        f"Сегодня: {money(stats.today_cost)}",
    ]
    if stats.unconfirmed_cost > 0:
        lines.append(f"Из них по оценке (озвучка, прерванные запросы): {money(stats.unconfirmed_cost)}")
    if stats.by_model:
        lines += ["", "По моделям:"]
        lines += [f"• {m.model_id}: {m.requests} — {money(m.cost_usd)}" for m in stats.by_model]
    lines += ["", f"Всего сообщений в истории: {stats.turns_total}, разговоров: {stats.conversations_total}"]
    return "\n".join(lines)

MEMORY_EMPTY = "Пока я ничего не запомнил надолго. Расскажите о себе или скажите «запомни, что…»."
MEMORY_HINT = "Запомнить: /remember текст. Записи можно исправить или удалить в «📋 Записи»."
MEMORY_DISABLED = "Долгосрочная память выключена (MEMORY_ENABLED=0)."
REMEMBER_USAGE = "Напишите, что запомнить: /remember Я веду проект «Кофейня», дедлайн 15 ноября"
REMEMBERED = "✅ Запомнил."
REMEMBER_REJECTED = (
    "Не сохранил: текст пустой, слишком длинный (больше 300 символов) или похож на секрет "
    "(пароль, номер карты, телефон, адрес). Такое в память не записываю."
)
MEMORY_DELETED = "Удалено"
MEMORY_NOT_FOUND = "Запись не найдена"
MEMORY_EDITED = "✅ Исправил запись."
MEMORY_SEARCH_PROMPT = "Что найти в памяти? Напишите слово или фразу. Отмена — /cancel."
MEMORY_SEARCH_NOTHING = "Ничего не нашёл в памяти по этому запросу."
MEMORY_EXPORTED = "📤 Вся сохранённая память в файле JSON."
MEMORY_CLEAR_CONFIRM = "Очистить всю долгосрочную память (факты, проекты, договорённости, эпизоды)? Переписка и настройки останутся."
MEMORY_CLEARED = "🗑 Память очищена."
WIPE_CONFIRM = (
    "⚠️ Удалить ВСЕ ваши данные: память, всю переписку, настройки и журнал действий? "
    "Это нельзя отменить. Учёт расходов останется без привязки к вам."
)
WIPED = "Все ваши данные удалены. Чтобы начать заново — /start."


def memory_edit_prompt(current: str) -> str:
    return f"Сейчас записано: «{current}»\n\nНапишите исправленный вариант одним сообщением. Отмена — /cancel."

VOICE_DISABLED = "🎙 Голос выключен в настройках (VOICE_ENABLED=0). Пока напишите текстом."
VOICE_TOO_LONG = "Голосовое слишком длинное (больше {limit} с). Разбейте на части или напишите текстом."
VOICE_NOT_HEARD = "Не расслышал — в сообщении нет речи или она неразборчива. Повторите, пожалуйста."
VOICE_FAILED = "🎙 Не получилось распознать голосовое: сервис временно недоступен. Попробуйте ещё раз или напишите текстом."
VOICE_BUDGET = "💸 Месячный лимит расходов исчерпан — голос сейчас недоступен."
TTS_FAILED = "(Озвучить ответ не получилось — ниже текстом.)"
TTS_TRUNCATED = "Озвучил начало ответа, полностью — текстом:"
VOICE_SAMPLE_TEXT = "Привет! Так звучит мой голос. Скажи, если хочешь говорить быстрее, медленнее или в другой манере."
VOICE_MODE_TITLES = {"auto": "голосом на голосовые", "always": "всегда голосом", "never": "только текстом"}
STYLE_TITLES_VOICE = {"natural": "естественно", "calm": "спокойно", "friendly": "дружелюбно",
                      "energetic": "энергично", "business": "по-деловому"}


def heard(text: str) -> str:
    short = text if len(text) <= 300 else text[:300].rstrip() + "…"
    return f"🎙 «{short}»"


def voice_settings_text(*, mode: str, voice_title: str, speed: float, style: str | None, stt: str,
                        tts: str) -> str:
    lines = [
        "🎙 Голос", "",
        f"Отвечать: {VOICE_MODE_TITLES.get(mode, mode)}",
        f"Голос: {voice_title}",
        f"Скорость: {speed:g}×",
    ]
    if style is not None:
        lines.append(f"Манера: {STYLE_TITLES_VOICE.get(style, style)}")
    lines += ["", f"Распознавание: {stt}", f"Синтез: {tts}",
              "", "Отправьте голосовое — я пойму и отвечу. «🔊 Пример» — послушать выбранный голос."]
    return "\n".join(lines)

BTN_REMINDERS = "⏰ Напоминания"
BTN_TASKS = "📋 Задачи"
TASKS_EMPTY = "📋 Задач пока нет. Просто напишите или надиктуйте: «добавь задачу: …» или целый план — я разложу по разделам."
TASKS_HEADER = "📋 Задачи в работе ({n}):"
TASKS_ALL_DONE = "Выполненных и отменённых: {n} — они есть в выгрузке Excel."
REMINDERS_EMPTY = "Активных напоминаний нет. Просто напишите или скажите: «напомни завтра в 10 позвонить врачу»."
REMINDERS_HEADER = "⏰ Ваши напоминания:"
ADD_REMINDER_PROMPT = "О чём и когда напомнить? Например: «завтра в 10:00 позвонить врачу» или «каждый понедельник в 9 проверить отчёты». Отмена — /cancel."
EDIT_REMINDER_PROMPT = "Что изменить в напоминании #{id}? Напишите новое время и/или текст. Отмена — /cancel."
REMINDER_DELETED = "🗑 Напоминание #{id} удалено."
REMINDER_NOT_FOUND = "Такого активного напоминания нет."
REMINDER_DONE = "✅ Отметил."
REMINDER_LATE = "(отправлено с опозданием — бот был выключен)"
REMINDER_SNOOZED = "⏰ Напомню {when}."
PROACTIVE_ON = "🔔 Инициативные сообщения включены ({freq}). Буду иногда писать первым — только по вашим делам и не в тихие часы ({quiet}). Выключить — /proactive_off."
PROACTIVE_OFF = "🔕 Инициативные сообщения выключены. Напоминания продолжат приходить. Включить — /proactive_on."
TIMEZONE_USAGE = "Укажите часовой пояс, например: /timezone Europe/Kyiv"
TIMEZONE_SET = "🌍 Часовой пояс: {tz}. Сейчас у вас {now}."
TIMEZONE_BAD = "Не знаю такого часового пояса. Примеры: Europe/Kyiv, Europe/Warsaw, Europe/Berlin, America/New_York."


def reminder_card(kind: str, view) -> str:  # type: ignore[no-untyped-def]
    rep = f" ({view.repeat})" if view.repeat else ""
    icon = {"created": "✅ Напоминание", "updated": "✏️ Изменено напоминание", "deleted": "🗑 Удалено напоминание"}[kind]
    return f"{icon} #{view.id} — {view.when}{rep}:\n{view.text}"


def minutes_to_hhmm(value: int) -> str:
    return f"{value // 60:02d}:{value % 60:02d}"


def quiet_text(start: int, end: int) -> str:
    return "выключены" if start == end else f"{minutes_to_hhmm(start)}–{minutes_to_hhmm(end)}"


# ---- вложения ------------------------------------------------------------------------------

NO_VISION_MODEL = ("🖼 Ни одна подключённая модель не умеет смотреть изображения. Выберите облачную модель "
                   "(Claude или GPT) в «🤖 Модель» или добавьте ключ.")
MEDIA_NO_COMMENT = "(Владелец прислал материалы без комментария.)"
TOO_MANY_FILES = "слишком много файлов за раз, этот не обработан"
FILES_FAILED_NOTE = "присланные файлы обработать не удалось: "
_KIND_ICONS = {"image": "🖼", "document": "📄", "audio": "🎧", "video": "🎬", "archive": "🗜"}


def file_too_big(limit_bytes: int, own_server: bool) -> str:
    mb = limit_bytes // (1024 * 1024)
    if own_server:
        return f"файл больше {mb} МБ (MAX_FILE_MB)"
    return (f"больше {mb} МБ — Telegram не отдаёт ботам такие файлы. Сожмите/обрежьте или пришлите "
            "ссылкой на облако вместе с нужным фрагментом")


def needs_receipt(views, rejected) -> bool:  # type: ignore[no-untyped-def]
    """Отчёт о получении нужен, если файлов несколько, что-то не удалось или это не одиночное фото."""
    if rejected or len(views) > 1:
        return True
    return any(v.kind != "image" or v.status != "ok" for v in views)


def media_receipt(views, rejected) -> str:  # type: ignore[no-untyped-def]
    ok = sum(1 for v in views if v.status == "ok")
    partial = sum(1 for v in views if v.status == "partial")
    failed = sum(1 for v in views if v.status == "failed") + len(rejected)
    head = f"📎 Получено: {len(views) + len(rejected)}"
    detail = [f"полностью {ok}"] if ok else []
    if partial:
        detail.append(f"частично {partial}")
    if failed:
        detail.append(f"не удалось {failed}")
    lines = [head + (" (" + ", ".join(detail) + ")" if detail else "")]
    for v in views[:25]:
        mark = {"ok": "✅", "partial": "⚠️", "failed": "❌"}.get(v.status, "•")
        info = "; ".join(v.facts[:4])
        line = f"{mark} {_KIND_ICONS.get(v.kind, '•')} #{v.id} {v.name}"
        if info:
            line += f" — {info}"
        if v.meta.get("scanned_pages"):
            line += " → текст сканов читаю по изображению страниц"
        if v.problems:
            line += "\n    ⚠️ " + "\n    ⚠️ ".join(v.problems[:4])
        lines.append(line)
    if len(views) > 25:
        lines.append(f"… и ещё {len(views) - 25}")
    for r in rejected[:15]:
        lines.append(f"❌ {r}")
    return "\n".join(lines)
