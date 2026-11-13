"""Запуск: python -m pai"""

from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramUnauthorizedError
from sqlalchemy.exc import SQLAlchemyError

from pai.bot import texts
from pai.bot.app import AppContext, build_dispatcher, set_commands
from pai.bot.sender import TelegramSender
from pai.bot.session import EnvAwareSession
from pai.config import ConfigError, Settings, load_settings
from pai.db import engine as db
from pai.llm.catalog import load_catalog
from pai.llm.checks import verify_catalog
from pai.llm.engine import ChatEngine, PydanticAIEngine
from pai.logging_setup import setup_logging
from pai.media.engine import MediaEngine
from pai.media.router import ModelRouter
from pai.media.store import AttachmentStore
from pai.memory.engine import MemoryEngine
from pai.ops import backup, doctor
from pai.proactive.service import ProactiveService
from pai.reminders.delivery import ReminderDelivery
from pai.reminders.service import ReminderService
from pai.services.assistant import Assistant
from pai.services.budget import BudgetGuard
from pai.services.limits import CircuitBreaker, RateLimiter
from pai.voice.engine import build_voice

log = logging.getLogger("pai")


def read_instructions(settings: Settings) -> str:
    try:
        return settings.instructions_file.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(f"не найден файл инструкций: {settings.instructions_file}") from exc


async def run(settings: Settings, *, engine: ChatEngine | None = None, verify: bool = True) -> None:
    catalog = load_catalog(settings.models_file)
    instructions = read_instructions(settings)
    await db.upgrade(settings.database_url)
    sql = db.make_engine(settings.database_url)
    sessions = db.make_sessionmaker(sql)
    session = (EnvAwareSession(api=TelegramAPIServer.from_base(settings.telegram_api_url))
               if settings.telegram_api_url else EnvAwareSession())
    bot = Bot(settings.bot_token.get_secret_value(), session=session)
    memory: MemoryEngine | None = None
    backups: asyncio.Task[None] | None = None
    loops: list[asyncio.Task[None]] = []
    dp = None
    try:
        try:
            me = await bot.get_me()
        except TelegramUnauthorizedError as exc:
            raise ConfigError("Telegram отклонил BOT_TOKEN — проверьте токен у @BotFather") from exc
        if verify:
            catalog = await verify_catalog(catalog, settings)
        chat_engine = engine or PydanticAIEngine(settings)
        budget = BudgetGuard(sessions, monthly_limit_usd=settings.monthly_budget_usd,
                             disabled=settings.paid_apis_disabled,
                             attempts_per_call=settings.llm_max_retries + 1)
        memory = (MemoryEngine(sessions=sessions, settings=settings, catalog=catalog,
                               engine=chat_engine, budget=budget)
                  if settings.memory_enabled else None)
        reminder_service = ReminderService(sessions)
        from pai.tasks.service import TaskService
        voice = build_voice(settings, budget)
        media: MediaEngine | None = None
        if settings.media_enabled:
            store = AttachmentStore(sessions, settings.media_dir,
                                    retention_hours=settings.media_retention_hours)
            media = MediaEngine(settings=settings, store=store, router=ModelRouter(catalog, settings),
                                voice=voice, engine=chat_engine, budget=budget)
            loops.append(asyncio.create_task(store.run_forever()))  # удаление файлов после срока хранения
        assistant = Assistant(
            sessions=sessions, settings=settings, catalog=catalog, engine=chat_engine, budget=budget,
            memory=memory, reminders=reminder_service, media=media, tasks=TaskService(sessions),
            rate_limiter=RateLimiter(settings.messages_per_minute),
            breaker=CircuitBreaker(settings.breaker_failure_threshold, settings.breaker_cooldown_seconds),
            base_instructions=instructions,
        )
        backups = asyncio.create_task(
            backup.run_forever(settings.database_url, settings.backup_dir, settings.backup_keep)
        )
        sender = TelegramSender(bot)
        # Два независимых цикла: доставка напоминаний (каждые 15 с) и инициатива (каждые 10 мин) —
        # долгая генерация инициативного сообщения не задерживает напоминание.
        delivery = ReminderDelivery(service=reminder_service, sessions=sessions, sender=sender,
                                    on_sent=assistant.record_outgoing, late_note=texts.REMINDER_LATE)
        loops.append(asyncio.create_task(delivery.run_forever()))
        if memory is not None:
            proactive = ProactiveService(
                sessions=sessions, settings=settings, catalog=catalog, engine=chat_engine, budget=budget,
                memory=memory.store, reminders=reminder_service,
                last_user_activity=assistant.last_user_activity,
            )
            loops.append(asyncio.create_task(proactive.run_forever(sender, assistant.record_outgoing)))
        # Контроль обучения в рабочей группе (config/training.toml). Изолирован от личных данных.
        from pai.bot.handlers.training import TrainingTelegramSender
        from pai.training.config import load_training_config
        from pai.training.scheduler import TrainingScheduler
        from pai.training.service import TrainingService

        tcfg = load_training_config()
        # Если для группы есть отдельный бот (TRAINING_BOT_TOKEN), личный бот группой не занимается.
        use_here = tcfg.enabled and not settings.training_bot_token
        training = TrainingService(sessions, tcfg) if use_here else None
        if training is not None:
            from pai.bot.handlers.training import SilenceGuard

            bot.session.middleware(SilenceGuard(training, set(settings.allowed_telegram_ids)))
            sender_t = TrainingTelegramSender(bot, settings.allowed_telegram_ids[0])
            loops.append(asyncio.create_task(TrainingScheduler(training, sender_t).run_forever()))
        dp = build_dispatcher(AppContext(settings, assistant, sessions, voice, media, training))
        await set_commands(bot, settings.allowed_telegram_ids, training=training is not None)
        await bot.delete_webhook(drop_pending_updates=False)
        available = ", ".join(m.id for m in catalog.available(settings))
        log.info("бот @%s запущен; доступ: %d ID; модели: %s; бюджет $%s, потрачено $%s",
                 me.username, len(settings.allowed_telegram_ids), available,
                 settings.monthly_budget_usd, await assistant.budget.spent_this_month())
        await dp.start_polling(
            bot, allowed_updates=["message", "edited_message", "callback_query", "chat_member"]
        )
    finally:
        if dp is not None and dp.get("collector") is not None:
            await dp["collector"].close()
        for loop_task in loops:
            loop_task.cancel()
        if backups is not None:
            backups.cancel()
        if memory is not None:
            await memory.wait()  # не обрывать запись памяти при остановке
        await bot.session.close()
        await sql.dispose()


async def run_group(settings: Settings) -> None:
    """Отдельный бот рабочей группы: только контроль обучения, своя база, без ИИ и личных данных."""
    from pai.bot.app import build_training_dispatcher
    from pai.bot.handlers.training import SilenceGuard, TrainingTelegramSender
    from pai.training.config import load_training_config
    from pai.training.scheduler import TrainingScheduler
    from pai.training.service import TrainingService

    if not settings.training_bot_token:
        raise ConfigError("не задан TRAINING_BOT_TOKEN — токен бота для рабочей группы")
    tcfg = load_training_config()
    await db.upgrade(settings.training_database_url)
    sql = db.make_engine(settings.training_database_url)
    sessions = db.make_sessionmaker(sql)
    bot = Bot(settings.training_bot_token.get_secret_value(), session=EnvAwareSession())
    owners = set(settings.allowed_telegram_ids)
    training = TrainingService(sessions, tcfg)
    bot.session.middleware(SilenceGuard(training, owners))
    loop_task: asyncio.Task[None] | None = None
    try:
        try:
            me = await bot.get_me()
        except TelegramUnauthorizedError as exc:
            raise ConfigError("Telegram отклонил TRAINING_BOT_TOKEN — проверьте токен у @BotFather") from exc
        sender = TrainingTelegramSender(bot, settings.allowed_telegram_ids[0])
        loop_task = asyncio.create_task(TrainingScheduler(training, sender).run_forever())
        dp = build_training_dispatcher(AppContext(settings, None, sessions, training=training))  # type: ignore[arg-type]
        from aiogram.types import BotCommandScopeChat

        from pai.bot.app import EMPLOYEE_COMMANDS, TRAINING_COMMANDS

        await bot.set_my_commands(EMPLOYEE_COMMANDS)
        for owner in owners:
            try:
                await bot.set_my_commands(TRAINING_COMMANDS, scope=BotCommandScopeChat(chat_id=owner))
            except TelegramBadRequest:
                log.warning("владелец ещё не нажал Start у бота группы — меню и отчёты появятся после этого")
        await bot.delete_webhook(drop_pending_updates=False)
        log.info("бот рабочей группы @%s запущен; тихий режим: %s; группа: %s",
                 me.username, await training.silent(), await training.group_id() or "не подключена")
        await dp.start_polling(bot, allowed_updates=["message", "edited_message", "chat_member"])
    finally:
        if loop_task is not None:
            loop_task.cancel()
        await bot.session.close()
        await sql.dispose()


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else "run"
    if command == "doctor":
        doctor.main()
        return
    if command not in ("run", "backup", "group"):
        print("Использование: python -m pai [run | group | doctor | backup]", file=sys.stderr)
        sys.exit(2)
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Ошибка настройки: {exc}\nОткройте файл .env (образец — .env.example).", file=sys.stderr)
        sys.exit(2)
    setup_logging(settings.log_level)
    if command == "backup":
        path = backup.backup_sqlite(settings.database_url, settings.backup_dir, settings.backup_keep)
        print(f"Резервная копия: {path}" if path else "Копия не нужна: база не SQLite или ещё не создана.")
        return
    try:
        asyncio.run(run_group(settings) if command == "group" else run(settings))
    except ConfigError as exc:
        print(f"Ошибка настройки: {exc}", file=sys.stderr)
        sys.exit(2)
    except TelegramNetworkError as exc:
        log.error("нет связи с Telegram: %s", exc)
        sys.exit(1)
    except (OSError, SQLAlchemyError) as exc:
        log.error("ошибка базы данных или сети: %s", type(exc).__name__)
        sys.exit(1)
    except KeyboardInterrupt:
        pass
    except Exception:
        log.critical("бот остановлен из-за непредвиденной ошибки", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
