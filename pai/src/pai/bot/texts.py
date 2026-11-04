"""Тексты интерфейса."""

from __future__ import annotations

from decimal import Decimal

BTN_NEW = "🆕 Новый диалог"
BTN_MODEL = "🤖 Модель"
BTN_STATS = "📊 Статистика"
BTN_SETTINGS = "⚙️ Настройки"
BTN_HELP = "❓ Помощь"
MENU_BUTTONS = (BTN_NEW, BTN_MODEL, BTN_STATS, BTN_SETTINGS, BTN_HELP)

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
    "Что я умею сейчас:\n"
    "• отвечать на вопросы и помогать с текстами, планами, идеями;\n"
    "• помнить контекст текущего разговора (до 20 последних обменов);\n"
    "• работать на разных моделях — «🤖 Модель»;\n"
    "• следить за расходами — «📊 Статистика».\n\n"
    "Скоро (следующие этапы):\n"
    "• долгосрочная память о вас и ваших проектах;\n"
    "• голосовые сообщения — понимать и отвечать голосом;\n"
    "• анализ PDF, Word, Excel, картинок;\n"
    "• инструменты: поиск в интернете, календарь, почта, GitHub — с подтверждением важных действий.\n\n"
    "Команды: /new — новый диалог, /model — модель, /stats — статистика, /settings — настройки, /menu — меню."
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

NOT_YET_VOICE = "🎙 Голосовые сообщения появятся на следующем этапе. Пока напишите текстом."
NOT_YET_FILES = "📎 Анализ файлов и изображений появится на одном из следующих этапов. Пока опишите задачу текстом."
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
        lines.append(f"Из них оценкой (ответ не дошёл): {money(stats.unconfirmed_cost)}")
    if stats.by_model:
        lines += ["", "По моделям:"]
        lines += [f"• {m.model_id}: {m.requests} — {money(m.cost_usd)}" for m in stats.by_model]
    lines += ["", f"Всего сообщений в истории: {stats.turns_total}, разговоров: {stats.conversations_total}"]
    return "\n".join(lines)
