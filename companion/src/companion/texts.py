"""Тексты интерфейса бота (не реплики персонажа — их генерирует модель)."""

from __future__ import annotations

from companion.persona import Persona
from companion.repositories.memory import Fact

AI_DISCLOSURE = (
    "{name} — виртуальная девушка, AI-персонаж. Её ответы генерирует искусственный интеллект, "
    "она не реальный человек и может ошибаться."
)


def welcome(persona: Persona) -> str:
    return (
        f"{persona.greeting}\n\n"
        f"⚠️ {AI_DISCLOSURE.format(name=persona.name)}\n\n"
        "Бот предназначен только для совершеннолетних. Нажимая кнопку ниже, ты подтверждаешь, "
        "что тебе есть 18 лет и ты понимаешь, что общаешься с AI. "
        "Как хранятся данные — /privacy."
    )


AGE_BUTTON = "Мне есть 18, начнём"
AGE_REQUIRED = "Сначала подтверди, что тебе есть 18 лет, — кнопка под приветствием. Или нажми /start."


def welcome_back(persona: Persona) -> str:
    return f"{persona.name} на связи. Пиши, о чём хочется поговорить. Команды — /help."


def after_confirm(persona: Persona) -> str:
    return f"Отлично! Можешь просто написать {persona.name} что-нибудь — например, как прошёл день."


def help_text(persona: Persona, support_contact: str) -> str:
    lines = [
        f"Это чат с {persona.name} — виртуальным AI-персонажем. Просто пиши сообщения, "
        "она отвечает и запоминает важное о тебе, чтобы разговор был живым.",
        "",
        "/reset — начать разговор заново (память о тебе сохранится)",
        "/memory — что она о тебе запомнила",
        "/forget — стереть память или удалить все данные",
        "/privacy — какие данные хранятся",
        "/help — эта справка",
        "",
        "Пока поддерживаются только текстовые сообщения.",
    ]
    if support_contact:
        lines += ["", f"Поддержка: {support_contact}"]
    return "\n".join(lines)


def privacy_text(persona: Persona, retention_days: int) -> str:
    return (
        "Какие данные хранятся\n\n"
        "• Твой Telegram ID и имя из профиля — чтобы отличать тебя от других пользователей.\n"
        "• Текст переписки — для контекста разговора. Номера карт, телефоны, e-mail и коды "
        "автоматически маскируются перед сохранением. Старая переписка сжимается в краткое "
        f"резюме, а исходные сообщения удаляются через {retention_days} дней.\n"
        f"• Факты, которые {persona.name} запомнила (/memory), — например, твои интересы.\n"
        "• Технические данные о расходе токенов без текста сообщений.\n\n"
        f"Чтобы ответить, текст сообщений передаётся в Claude API (Anthropic). "
        "Твои данные не передаются другим пользователям и не используются для рекламы.\n\n"
        "Не отправляй в чат пароли, коды из SMS, данные карт и документов.\n\n"
        "Управление: /memory — посмотреть, /forget — стереть память или удалить всё."
    )


def memory_text(persona: Persona, facts: list[Fact], summaries: int, messages: int) -> str:
    if not facts and not summaries:
        body = f"{persona.name} пока ничего не запомнила о тебе надолго."
    else:
        titles = {
            "name": "Обращение",
            "interest": "Интересы",
            "preference": "Предпочтения",
            "fact": "Факты",
            "thread": "Темы, к которым можно вернуться",
        }
        blocks = []
        for cat, title in titles.items():
            items = [f.text for f in facts if f.category == cat]
            if items:
                blocks.append(title + ":\n" + "\n".join(f"• {i}" for i in items))
        if summaries:
            blocks.append(f"Кратких резюме прошлых разговоров: {summaries}")
        body = f"Что {persona.name} помнит о тебе:\n\n" + "\n\n".join(blocks)
    return f"{body}\n\nСообщений в истории: {messages}.\nСтереть — /forget."


FORGET_MENU = (
    "Что удалить?\n\n"
    "• Память и историю — факты о тебе, резюме и всю переписку. Чат продолжится с чистого листа.\n"
    "• Все мои данные — полностью удалить аккаунт из бота (потребуется снова нажать /start)."
)
FORGET_MEMORY_BUTTON = "Память и историю"
FORGET_ALL_BUTTON = "Все мои данные"
CANCEL_BUTTON = "Отмена"
CONFIRM_BUTTON = "Да, удалить"
FORGET_MEMORY_CONFIRM = "Точно стереть память и всю историю переписки? Это нельзя отменить."
FORGET_ALL_CONFIRM = "Точно удалить все твои данные из бота? Это нельзя отменить."
FORGET_MEMORY_DONE = "Готово: память и история стёрты. Можно начать знакомство заново."
FORGET_ALL_DONE = "Готово: все твои данные удалены. Если захочешь вернуться — /start."
CANCELLED = "Отменено, ничего не удалено."
RESET_DONE = (
    "Начинаем разговор с чистого листа. То, что я о тебе запомнила, осталось — "
    "посмотреть можно в /memory."
)

ONLY_TEXT = "Пока я умею только переписываться текстом 🙂"
TOO_FAST = "Ты пишешь очень быстро — дай мне минутку, чтобы успеть ответить."
DAILY_LIMIT = "На сегодня лимит сообщений исчерпан. Давай продолжим завтра!"
BUDGET_EXHAUSTED = "Сервис временно приостановлен по техническим причинам. Попробуй позже."
TEMPORARY_ERROR = "Что-то связь барахлит, я не смогла ответить. Напиши ещё раз через минуту."
UNAVAILABLE = "Сервис сейчас недоступен. Мы уже разбираемся — попробуй позже."
REFUSED = "Давай лучше поговорим о чём-нибудь другом."
INTERNAL_ERROR = "Произошла ошибка. Попробуй ещё раз чуть позже."
