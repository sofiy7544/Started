"""Контроль обучения в Telegram: записи в рабочей группе, оценки владельца, ответы сотрудников,
команды владельца и /my для сотрудников.

Изоляция: обработчики используют только TrainingService (таблицы training_*) и тексты из
config/training.toml. Личный ассистент владельца (память, задачи, переписка, ИИ) здесь недоступен.
Роутер стоит первым: всё из группы и всё от сотрудников в личке заканчивается здесь.
"""

from __future__ import annotations

import logging
import re
from contextvars import ContextVar
from datetime import date, timedelta
from html import escape

from aiogram import Bot, F, Router
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, Filter
from aiogram.types import ChatMemberUpdated, Message, ReactionTypeEmoji

from pai.bot.app import AppContext, OwnerOnly, is_owner, split_message
from pai.training import reports
from pai.training.config import WEEKDAY_SHORT
from pai.training.parser import parse_caption
from pai.training.scheduler import mention

log = logging.getLogger(__name__)
GROUPS = (ChatType.GROUP, ChatType.SUPERGROUP)
MEDIA_EXT = {"mp3", "m4a", "ogg", "oga", "opus", "wav", "aac", "flac", "amr", "mp4", "mov", "3gp", "webm", "mkv", "avi"}


def _owner_id(app: AppContext) -> int:
    return app.settings.allowed_telegram_ids[0]


def record_media(message: Message) -> tuple[str, str] | None:
    """(вид, file_id) для записи разговора; None — это не запись."""
    for kind in ("voice", "audio", "video", "video_note"):
        obj = getattr(message, kind)
        if obj is not None:
            return kind, obj.file_id
    d = message.document
    if d is not None:
        mime = (d.mime_type or "").lower()
        ext = (d.file_name or "").rsplit(".", 1)[-1].lower() if "." in (d.file_name or "") else ""
        if mime.startswith(("audio/", "video/")) or ext in MEDIA_EXT:
            return "document", d.file_id
    return None


def message_link(message: Message) -> str | None:
    chat = message.chat
    if chat.username:
        return f"https://t.me/{chat.username}/{message.message_id}"
    s = str(chat.id)
    return f"https://t.me/c/{s[4:]}/{message.message_id}" if s.startswith("-100") else None


async def _react(bot: Bot, message: Message, app: AppContext) -> None:
    if await app.training.silent():
        return
    cfg = app.training.config
    try:
        await bot.set_message_reaction(message.chat.id, message.message_id,
                                       reaction=[ReactionTypeEmoji(emoji=cfg.text("accepted_reaction"))])
    except TelegramAPIError:
        await message.reply(cfg.text("accepted"))  # реакции выключены в группе — коротким ответом


async def _to_owner(bot: Bot, app: AppContext, html: str) -> None:
    for part in split_message(html):
        await bot.send_message(_owner_id(app), part, parse_mode="HTML")


def _display_name(message: Message) -> str:
    u = message.from_user
    return " ".join(x for x in (u.first_name, u.last_name) if x) or (u.username or str(u.id))


async def _pinged_without_answer(app: AppContext, person_id: int, day: date) -> bool:
    svc = app.training
    return (await svc.has_job("ping", f"{day}:{person_id}") and not (await svc.reasons_on(day)).get(person_id)
            and not await svc.count_on(person_id, day))


# ---- группа --------------------------------------------------------------------------------


async def group_message(message: Message, bot: Bot, app: AppContext) -> None:
    svc = app.training
    u = message.from_user
    if u is None or u.is_bot:
        return
    if message.text and message.text.split()[0].split("@")[0] == "/bind" and is_owner(app, u.id):
        await svc.set_group(message.chat.id)
        note = " Тихий режим включён: в группе я молчу и только записываю. Включить ответы — /silent off." \
            if await svc.silent() else ""
        title = f" «{escape(message.chat.title)}»" if message.chat.title else ""
        await _to_owner(bot, app, f"Группа{title} подключена.{note}")
        return
    if message.chat.id != await svc.group_id():
        return  # чужие группы — молча игнорируем
    # Вступили / вышли — запоминаем (Telegram не даёт боту список участников группы).
    if message.new_chat_members or message.left_chat_member:
        for m in message.new_chat_members or []:
            if not m.is_bot and not is_owner(app, m.id):
                await svc.touch(m.id, " ".join(x for x in (m.first_name, m.last_name) if x), m.username, employee=True)
        left = message.left_chat_member
        if left is not None and not left.is_bot:
            p = await svc.person_by_tg(left.id)
            if p is not None:
                await svc.set_employee(p.id, False, by_owner=False)
        return
    if is_owner(app, u.id):
        await _owner_in_group(message, bot, app)
        return
    # Любой участник, написавший в группе, — в списке сотрудников (если владелец его не убирал).
    person = await svc.touch(u.id, _display_name(message), u.username, employee=True)
    media = record_media(message)
    cfg = svc.config
    if media is not None:
        kind, file_id = media
        cap = parse_caption(message.caption)
        rec = await svc.add_record(person, cap, kind=kind, file_id=file_id, chat_id=message.chat.id,
                                   message_id=message.message_id, link=message_link(message), sent_at=message.date)
        if await svc.silent():
            return  # тихий режим: записываем молча
        if rec.result is None:
            await message.reply(cfg.text("ask_caption"))
        elif rec.result == "rescheduled" and rec.reschedule_at is None:
            await message.reply(cfg.text("ask_reschedule_date"))
        else:
            await _react(bot, message, app)
        return
    text = (message.text or message.caption or "").strip()
    if not text:
        return
    # Подпись ответом на свою запись без подписи.
    replied = message.reply_to_message
    if replied is not None and replied.from_user and replied.from_user.id == u.id:
        rec = await svc.record_by_message(message.chat.id, replied.message_id)
        if rec is not None and rec.result is None:
            cap = parse_caption(text)
            if cap.complete:
                await svc.update_caption(message.chat.id, replied.message_id, cap)
                await _react(bot, replied, app)
            elif not await svc.silent():
                await message.reply(cfg.text("ask_caption"))
            return
    day = svc.local_now().date()
    if await _pinged_without_answer(app, person.id, day):
        await svc.add_reason(person.id, day, text, "group")
        await _to_owner(bot, app, f"📝 {escape(person.name)} — причина, почему нет записей:\n{escape(text)}")
        await message.reply(cfg.text("reason_saved"))
        return
    me = await bot.me()
    addressed = (replied is not None and replied.from_user and replied.from_user.id == me.id) or (
        me.username and f"@{me.username.lower()}" in text.lower())
    if addressed:
        await _to_owner(bot, app, f"❓ Вопрос от {escape(person.name)} (из группы):\n{escape(text)}")
        if not await svc.silent():
            await message.reply(cfg.text("question_redirect"))


async def _owner_in_group(message: Message, bot: Bot, app: AppContext) -> None:
    """Ответ владельца реплаем на запись — оценка и комментарий к этой записи."""
    replied = message.reply_to_message
    text = (message.text or message.caption or "").strip()
    if replied is None or not text:
        return
    rec = await app.training.record_by_message(message.chat.id, replied.message_id)
    if rec is None:
        return
    await app.training.score(rec.id, text)
    if await app.training.silent():
        return
    try:  # знак, что оценка сохранена; к комментарию бот ничего не дописывает
        await bot.set_message_reaction(message.chat.id, message.message_id, reaction=[ReactionTypeEmoji(emoji="✍")])
    except TelegramAPIError:
        pass


async def member_update(event: ChatMemberUpdated, app: AppContext) -> None:
    """Вступление/выход участника (приходит, если бот — админ группы)."""
    svc = app.training
    if event.chat.id != await svc.group_id():
        return
    u = event.new_chat_member.user
    if u.is_bot or is_owner(app, u.id):
        return
    status = event.new_chat_member.status
    if status in ("member", "restricted", "administrator", "creator"):
        await svc.touch(u.id, " ".join(x for x in (u.first_name, u.last_name) if x), u.username, employee=True)
    elif status in ("left", "kicked"):
        p = await svc.person_by_tg(u.id)
        if p is not None:
            await svc.set_employee(p.id, False, by_owner=False)


async def group_edited(message: Message, bot: Bot, app: AppContext) -> None:
    """Сотрудник дописал подпись правкой сообщения."""
    svc = app.training
    if message.chat.id != await svc.group_id() or message.from_user is None or is_owner(app, message.from_user.id):
        return
    rec = await svc.record_by_message(message.chat.id, message.message_id)
    if rec is None or rec.result is not None:
        return
    cap = parse_caption(message.caption)
    if cap.complete and await svc.update_caption(message.chat.id, message.message_id, cap):
        await _react(bot, message, app)


# ---- личка сотрудника -----------------------------------------------------------------------


async def employee_private(message: Message, bot: Bot, app: AppContext) -> None:
    svc = app.training
    cfg = svc.config
    u = message.from_user
    person = await svc.person_by_tg(u.id)
    if person is None:
        await message.answer(cfg.text("not_employee"))
        return
    text = (message.text or message.caption or "").strip()
    command = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
    if await svc.silent():
        # Тихий режим: сотруднику не отвечаем; вопрос (не команду) молча передаём владельцу.
        await svc.set_dm_ok(person.id)
        if text and not command:
            await _to_owner(bot, app, f"❓ {escape(person.name)} написал боту в личку (тихий режим, ответа не было):\n"
                                      f"{escape(text)}")
        return
    if command == "/start":
        await svc.set_dm_ok(person.id)
        await message.answer(cfg.text("employee_start"))
        return
    if command == "/my":
        await svc.set_dm_ok(person.id)
        await message.answer(await reports.my_stats(svc, person), parse_mode="HTML")
        return
    if command == "/tips":
        await message.answer(cfg.text("tips"))
        return
    await svc.set_dm_ok(person.id)
    if not text:
        await message.answer(cfg.text("employee_start"))
        return
    day = svc.local_now().date()
    if await _pinged_without_answer(app, person.id, day):
        await svc.add_reason(person.id, day, text, "dm")
        await _to_owner(bot, app, f"📝 {escape(person.name)} — причина, почему нет записей:\n{escape(text)}")
        await message.answer(cfg.text("reason_saved"))
        return
    await _to_owner(bot, app, f"❓ Вопрос от {escape(person.name)} (в личке):\n{escape(text)}")
    await message.answer(cfg.text("question_redirect"))


# ---- команды владельца -----------------------------------------------------------------------


async def _resolve(message: Message, app: AppContext, query: str, *, employees_only: bool = False):  # type: ignore[no-untyped-def]
    found = await app.training.find(query, employees_only=employees_only)
    if not found:
        await message.answer(f"Не нашёл «{escape(query)}». Список: /staff", parse_mode="HTML")
        return None
    if len(found) > 1:
        names = ", ".join(escape(p.name) + (f" (@{p.username})" if p.username else "") for p in found[:10])
        await message.answer(f"Нашлось несколько: {names}. Уточни имя или @username.", parse_mode="HTML")
        return None
    return found[0]


async def cmd_today(message: Message, app: AppContext) -> None:
    await _send_html(message, await reports.daily_report(app.training, app.training.local_now().date()))


async def cmd_week(message: Message, app: AppContext) -> None:
    await _send_html(message, await reports.weekly_report(app.training, app.training.local_now().date()))


async def cmd_employee(message: Message, command: CommandObject, app: AppContext) -> None:
    if not command.args:
        await message.answer("Использование: /employee Имя")
        return
    p = await _resolve(message, app, command.args)
    if p is not None:
        await _send_html(message, await reports.employee_history(app.training, p))


async def cmd_staff(message: Message, app: AppContext) -> None:
    people = await app.training.employees()
    if not people:
        await message.answer("Сотрудников пока нет: человек попадает в список после первой записи в группе или командой /add.")
        return
    lines = [f"• {escape(p.name)}" + (f" (@{p.username})" if p.username else "") + ("" if p.dm_ok else " — бота в личке не запускал")
             for p in people]
    await _send_html(message, "👥 Сотрудники:\n" + "\n".join(lines))


async def cmd_add_remove(message: Message, command: CommandObject, app: AppContext) -> None:
    adding = command.command == "add"
    target = None
    if message.reply_to_message is not None and message.reply_to_message.from_user is not None:
        ru = message.reply_to_message.from_user
        if not ru.is_bot and not is_owner(app, ru.id):
            target = await app.training.touch(ru.id, " ".join(x for x in (ru.first_name, ru.last_name) if x), ru.username)
    elif command.args:
        target = await _resolve(message, app, command.args)
    else:
        await message.answer(f"Использование: /{command.command} Имя или @username (человек должен хоть раз написать "
                             "в группе), или ответьте этой командой на его сообщение в группе.")
        return
    if target is None:
        return
    await app.training.set_employee(target.id, adding)
    note = f"{'Добавлен в список' if adding else 'Убран из списка'}: {escape(target.name)}"
    if message.chat.type == ChatType.PRIVATE:
        await message.answer(note, parse_mode="HTML")
    else:  # команда в группе — подтверждение только владельцу в личку, в группе тишина
        await message.bot.send_message(message.from_user.id, note, parse_mode="HTML")


_DATE = re.compile(r"(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?")


def _parse_dates(text: str, today: date) -> tuple[date, date] | None:
    s = text.lower()
    if "сегодня" in s or "сьогодні" in s:
        return today, today
    if "завтра" in s:
        d = today + timedelta(days=1)
        return d, d
    found = []
    for m in _DATE.finditer(s):
        year = int(m.group(3)) if m.group(3) else today.year
        year += 2000 if year < 100 else 0
        try:
            found.append(date(year, int(m.group(2)), int(m.group(1))))
        except ValueError:
            return None
    if not found:
        return None
    return found[0], found[-1]


async def cmd_off(message: Message, command: CommandObject, app: AppContext) -> None:
    args = (command.args or "").strip()
    m = re.search(r"(сегодня|сьогодні|завтра|\d{1,2}[./]\d{1,2})", args.lower())
    if not args or not m:
        await message.answer("Использование: /off Имя 15.11 или /off Имя 15.11-20.11 (или «сегодня», «завтра»)")
        return
    name, dates = args[:m.start()].strip(), args[m.start():]
    rng = _parse_dates(dates, app.training.local_now().date())
    if not name or rng is None:
        await message.answer("Не понял имя или даты. Пример: /off Иван 15.11-20.11")
        return
    p = await _resolve(message, app, name, employees_only=True)
    if p is None:
        return
    await app.training.add_absence(p.id, *rng)
    period = f"{rng[0]:%d.%m}" + (f"–{rng[1]:%d.%m}" if rng[1] != rng[0] else "")
    await message.answer(f"Выходной/отпуск: {escape(p.name)}, {period}. Проверки в эти дни его не касаются.",
                         parse_mode="HTML")


_DAYS = {name: i for i, name in enumerate(WEEKDAY_SHORT)}


async def cmd_schedule(message: Message, command: CommandObject, app: AppContext) -> None:
    svc = app.training
    args = (command.args or "").strip().lower()
    usage = ("\n\nИзменить:\n/schedule hours 10:00-19:00\n/schedule days пн-сб (или пн,вт,чт)\n/schedule check 13:00\n"
             "/schedule idle 2 (дней без записей до упоминания)\n/schedule followup 60\n/schedule daily 19:30\n/schedule weekly вс 20:00")
    if not args:
        await message.answer((await svc.schedule()).describe() + usage)
        return
    key, _, value = args.partition(" ")
    value = value.strip()
    try:
        if key == "hours":
            a, b = re.split(r"\s*[-–]\s*", value)
            changes: dict = {"work_start": a, "work_end": b}
        elif key == "days":
            if re.fullmatch(r"\w\w\s*[-–]\s*\w\w", value):
                a, b = re.split(r"\s*[-–]\s*", value)
                changes = {"work_days": list(range(_DAYS[a], _DAYS[b] + 1))}
            else:
                changes = {"work_days": [_DAYS[d.strip()] for d in re.split(r"[,\s]+", value) if d.strip()]}
        elif key == "check":
            changes = {"first_check": value}
        elif key == "idle":
            changes = {"idle_days": int(value)}
        elif key == "followup":
            changes = {"followup_minutes": int(value)}
        elif key == "daily":
            changes = {"daily_report": value}
        elif key == "weekly":
            d, t = value.split()
            changes = {"weekly_report_day": _DAYS[d], "weekly_report": t}
        else:
            raise ValueError(key)
        new = await svc.update_schedule(changes)
    except Exception:  # noqa: BLE001 — любая ошибка ввода: показываем, как правильно
        await message.answer("Не понял. " + usage.strip())
        return
    await message.answer("Сохранено.\n" + new.describe())


async def cmd_silent(message: Message, command: CommandObject, app: AppContext) -> None:
    svc = app.training
    arg = (command.args or "").strip().lower()
    if arg in ("on", "вкл", "да"):
        await svc.set_silent(True)
    elif arg in ("off", "выкл", "нет"):
        await svc.set_silent(False)
    elif arg:
        await message.answer("Использование: /silent on — молчать, /silent off — отвечать")
        return
    if await svc.silent():
        await message.answer("🔇 Тихий режим включён: в группе и сотрудникам бот ничего не пишет и не отвечает, "
                             "только молча записывает записи и оценки. Отчёты и команды — вам.\n"
                             "Включить ответы и проверки: /silent off")
    else:
        await message.answer("🔊 Бот отвечает в группе: просит подписи, ставит реакции, упоминает тех, у кого "
                             "нет записей 2 рабочих дня, напоминает о переносах.\nВыключить: /silent on")


async def cmd_announce(message: Message, command: CommandObject, bot: Bot, app: AppContext) -> None:
    group = await app.training.group_id()
    if group is None:
        await message.answer("Группа не подключена: добавьте бота в группу и напишите там /bind.")
        return
    if not command.args:
        await message.answer("Использование: /announce текст сообщения для группы")
        return
    token = ANNOUNCE.set(True)  # явная команда владельца — разрешена и в тихом режиме
    try:
        await bot.send_message(group, command.args)  # дословно, без добавлений
    finally:
        ANNOUNCE.reset(token)
    await message.answer("Отправлено в группу.")


async def _send_html(message: Message, html: str) -> None:
    for part in split_message(html):
        await message.answer(part, parse_mode="HTML", disable_web_page_preview=True)


class NotOwner(Filter):
    async def __call__(self, event, app: AppContext) -> bool:  # type: ignore[no-untyped-def,override]
        return event.from_user is not None and not is_owner(app, event.from_user.id)


def create_router() -> Router:
    r = Router(name="training")
    owner_private = (F.chat.type == ChatType.PRIVATE, OwnerOnly())
    r.message.register(cmd_today, *owner_private, Command("today"))
    r.message.register(cmd_week, *owner_private, Command("week"))
    r.message.register(cmd_employee, *owner_private, Command("employee"))
    r.message.register(cmd_staff, *owner_private, Command("staff"))
    r.message.register(cmd_add_remove, OwnerOnly(), Command("add", "remove"))
    r.message.register(cmd_off, *owner_private, Command("off"))
    r.message.register(cmd_schedule, *owner_private, Command("schedule"))
    r.message.register(cmd_announce, *owner_private, Command("announce"))
    r.message.register(cmd_silent, *owner_private, Command("silent"))
    r.message.register(group_message, F.chat.type.in_(GROUPS))
    r.edited_message.register(group_edited, F.chat.type.in_(GROUPS))
    r.chat_member.register(member_update, F.chat.type.in_(GROUPS))
    r.message.register(employee_private, F.chat.type == ChatType.PRIVATE, NotOwner())
    return r


ANNOUNCE: ContextVar[bool] = ContextVar("training_announce", default=False)
_OUTGOING = {"SendMessage", "SendVoice", "SendDocument", "SendPhoto", "SendVideo", "SendAudio", "SendVideoNote",
             "SendSticker", "SendAnimation", "SendMediaGroup", "SendLocation", "SendContact", "SendPoll", "SendDice",
             "ForwardMessage", "ForwardMessages", "CopyMessage", "CopyMessages", "SetMessageReaction",
             "SendChatAction", "PinChatMessage"}


class SilenceGuard(BaseRequestMiddleware):
    """Страховка тихого режима на выходе из бота: всё, что адресовано не владельцу (группа,
    сотрудники, посторонние), не отправляется. Исключение — /announce владельца."""

    def __init__(self, training, owner_ids: set[int]) -> None:  # type: ignore[no-untyped-def]
        self._training = training
        self._owners = owner_ids

    async def __call__(self, make_request, bot, method):  # type: ignore[no-untyped-def]
        chat_id = getattr(method, "chat_id", None)
        if (type(method).__name__ in _OUTGOING and chat_id is not None and chat_id not in self._owners
                and not ANNOUNCE.get() and await self._training.silent()):
            log.info("silent mode: blocked %s to %s", type(method).__name__, chat_id)
            return True if type(method).__name__ in ("SetMessageReaction", "SendChatAction", "PinChatMessage") else None
        return await make_request(bot, method)


OWNER_HELP = ("Это бот рабочей группы (контроль записей сотрудников). Личный ассистент — в другом боте.\n\n"
              "Команды: /today, /week, /employee Имя, /staff, /add, /remove, /off Имя даты, /schedule, "
              "/announce текст, /silent on|off.\nПодключить группу: сделайте бота админом и напишите в группе /bind.")


async def owner_help(message: Message, bot: Bot) -> None:
    from aiogram.types import BotCommandScopeChat

    from pai.bot.app import TRAINING_COMMANDS

    await message.answer(OWNER_HELP)
    try:  # меню команд владельцу — после первого Start (раньше Telegram не даёт)
        await bot.set_my_commands(TRAINING_COMMANDS, scope=BotCommandScopeChat(chat_id=message.chat.id))
    except TelegramAPIError:
        pass


def create_owner_help_router() -> Router:
    """Для отдельного бота группы: на всё прочее от владельца в личке — подсказка по командам."""
    r = Router(name="training_owner_help")
    r.message.register(owner_help, F.chat.type == ChatType.PRIVATE, OwnerOnly())
    return r


class TrainingTelegramSender:
    """Отправка для планировщика: группа, владелец, сотрудник в личку."""

    def __init__(self, bot: Bot, owner_id: int) -> None:
        self.bot = bot
        self.owner_id = owner_id

    async def to_group(self, chat_id: int, html: str) -> None:
        await self.bot.send_message(chat_id, html, parse_mode="HTML")

    async def to_owner(self, html: str) -> None:
        for part in split_message(html):
            await self.bot.send_message(self.owner_id, part, parse_mode="HTML", disable_web_page_preview=True)

    async def to_person(self, tg_id: int, html: str) -> bool:
        try:
            await self.bot.send_message(tg_id, html, parse_mode="HTML")
            return True
        except TelegramAPIError:
            return False  # сотрудник не запускал бота или заблокировал его


__all__ = ["OwnerOnly", "TrainingTelegramSender", "create_router", "mention"]
