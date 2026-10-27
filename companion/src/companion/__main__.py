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

from companion import db, pricing
from companion.bot.app import AppContext, build_dispatcher, set_commands
from companion.config import ConfigError, Settings
from companion.llm.client import AnthropicLLM, LLMError, LLMErrorKind
from companion.logging_setup import setup_logging
from companion.persona import load_persona
from companion.services import maintenance
from companion.services.budget import BudgetGuard
from companion.services.chat import ChatService
from companion.services.limits import CircuitBreaker, RateLimiter
from companion.services.memory import MemoryService

log = logging.getLogger("companion")


def _check_anthropic_credentials() -> None:
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        raise ConfigError("Не задана переменная окружения ANTHROPIC_API_KEY")


async def check_models(llm: AnthropicLLM, settings: Settings) -> None:
    """Ключ и модели проверяются при старте через Models API — бесплатно, без расхода токенов."""
    for model in dict.fromkeys((settings.chat_model, settings.memory_model)):
        if not pricing.is_known(model):
            log.warning("цена модели %s неизвестна — бюджет считает её по максимальному тарифу", model)
        try:
            await llm.check_model(model)
        except LLMError as exc:
            if exc.kind is LLMErrorKind.AUTH:
                raise ConfigError(
                    "Anthropic отклонил ANTHROPIC_API_KEY — проверьте ключ в console.anthropic.com"
                ) from exc
            if exc.kind is LLMErrorKind.BAD_REQUEST:
                raise ConfigError(
                    f"Модель {model!r} недоступна для этого ключа — проверьте CHAT_MODEL / MEMORY_MODEL"
                ) from exc
            log.warning("Claude API сейчас недоступен, проверка модели %s пропущена: %s", model, exc)


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
    maintenance_task: asyncio.Task[None] | None = None
    try:
        await db.apply_migrations(pool)
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
        await check_models(llm, settings)
        maintenance_task = asyncio.create_task(
            maintenance.run_forever(pool, settings.message_retention_days)
        )
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
        if maintenance_task is not None:
            maintenance_task.cancel()
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
    setup_logging(settings.log_level)
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
    except Exception:
        # Трассировка без текста исключения (см. SafeFormatter), чтобы не вывести личные данные.
        log.critical("бот остановлен из-за непредвиденной ошибки", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
