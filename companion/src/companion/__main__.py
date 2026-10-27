"""Точка входа: python -m companion"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

import asyncpg
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError

from companion import db
from companion.bot.app import AppContext, build_dispatcher, set_commands
from companion.config import ConfigError, Settings
from companion.llm.client import AnthropicLLM
from companion.persona import load_persona
from companion.repositories import users
from companion.services.budget import BudgetGuard
from companion.services.chat import ChatService
from companion.services.limits import CircuitBreaker, RateLimiter
from companion.services.memory import MemoryService

log = logging.getLogger("companion")


def _check_anthropic_credentials() -> None:
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        raise ConfigError("Не задана переменная окружения ANTHROPIC_API_KEY")


async def run(settings: Settings) -> None:
    persona = load_persona(settings.persona_path)
    pool = await db.create_pool(settings.database_url)
    llm = AnthropicLLM(
        effort=settings.llm_effort,
        refusal_fallbacks=settings.llm_refusal_fallbacks,
        max_retries=settings.llm_max_retries,
        timeout=settings.llm_timeout_seconds,
    )
    session = (
        AiohttpSession(api=TelegramAPIServer.from_base(settings.telegram_api_url))
        if settings.telegram_api_url
        else None
    )
    bot = Bot(settings.bot_token, session=session)
    chat: ChatService | None = None
    try:
        await db.apply_migrations(pool)
        await users.prune_processed_updates(pool)
        budget = BudgetGuard(pool, settings)
        chat = ChatService(
            pool=pool,
            llm=llm,
            settings=settings,
            persona=persona,
            budget=budget,
            memory_service=MemoryService(pool, llm, budget, settings, persona.name),
            rate_limiter=RateLimiter(settings.messages_per_minute),
            breaker=CircuitBreaker(
                settings.breaker_failure_threshold, settings.breaker_cooldown_seconds
            ),
        )
        dp = build_dispatcher(AppContext(settings, persona, chat), pool)
        try:
            me = await bot.get_me()
        except TelegramUnauthorizedError as exc:
            raise ConfigError("Telegram отклонил BOT_TOKEN — проверьте токен у @BotFather") from exc
        log.info(
            "bot @%s started: chat_model=%s budget=%s USD, spent this month=%s USD",
            me.username, settings.chat_model, settings.anthropic_monthly_budget_usd,
            await budget.spent(),
        )
        await set_commands(bot)
        # Вебхук и long polling взаимоисключающие; накопленные апдейты не сбрасываем.
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        if chat is not None:
            await chat.wait_background()
        await bot.session.close()
        await llm.close()
        await pool.close()


def main() -> None:
    try:
        settings = Settings.from_env()
        _check_anthropic_credentials()
    except ConfigError as exc:
        print(f"Ошибка конфигурации: {exc}\nСм. .env.example", file=sys.stderr)
        sys.exit(2)
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx/aiogram на уровне INFO логируют URL запросов — в них есть токен бота.
    for noisy in ("httpx", "httpx2", "aiogram.event"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        asyncio.run(run(settings))
    except ConfigError as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        sys.exit(2)
    except TelegramNetworkError as exc:
        log.error("нет связи с Telegram Bot API: %s", exc)
        sys.exit(1)
    except (OSError, asyncpg.PostgresError) as exc:
        log.error("нет связи с базой данных: %s: %s", type(exc).__name__, exc)
        sys.exit(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
