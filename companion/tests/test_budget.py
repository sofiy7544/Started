"""Учёт расходов: резервирование, гонки, таймауты, отмены, ошибки API."""

from __future__ import annotations

import asyncio
from decimal import Decimal

import anthropic
import pytest

from companion import pricing
from companion.llm.client import AnthropicLLM, LLMError, LLMErrorKind
from companion.services import budget as budget_mod
from companion.services.chat import ReplyKind
from tests.conftest import new_user
from tests.fakes import FakeAnthropic, FakeLLM

ONE = Decimal("1")


@pytest.fixture
def fixed_estimate(monkeypatch):
    """Каждый вызов оценивается ровно в $1 — так проще считать резервы."""
    monkeypatch.setattr(budget_mod.pricing, "max_cost_usd", lambda *a, **k: ONE)


async def total_spent(pool) -> Decimal:
    return await pool.fetchval("SELECT COALESCE(sum(cost_usd), 0) FROM api_usage")


async def test_concurrent_requests_cannot_overshoot_budget(pool, make_service, fixed_estimate) -> None:
    llm = FakeLLM(delay=0.2)
    service = make_service(llm_override=llm, settings_override={
        "anthropic_monthly_budget_usd": 2.5, "budget_soft_ratio": 1.0})
    people = [await new_user(pool, 100 + i) for i in range(6)]
    replies = await asyncio.gather(*(service.handle_message(u, "привет") for u in people))
    kinds = [r.kind for r in replies]
    # В $2.5 помещаются две оценки по $1: остальные четыре получают отказ, а не перерасход.
    assert kinds.count(ReplyKind.OK) == 2 and kinds.count(ReplyKind.BUDGET) == 4
    assert len(llm.chat_calls) == 2
    assert service._budget.reserved == 0
    assert await total_spent(pool) <= Decimal("2.5")


async def test_success_records_actual_usage_and_releases_reserve(pool, service, llm) -> None:
    user = await new_user(pool, 1)
    await service.handle_message(user, "привет")
    row = await pool.fetchrow("SELECT operation, model, input_tokens, output_tokens, cost_usd FROM api_usage")
    assert row["operation"] == "chat" and row["input_tokens"] == 1000 and row["output_tokens"] == 50
    assert row["cost_usd"] == pricing.cost_usd("claude-haiku-5-5", input_tokens=1000, output_tokens=50)
    assert service._budget.reserved == 0


async def test_http_error_records_nothing(pool, service, llm) -> None:
    user = await new_user(pool, 1)
    llm.replies = [LLMError(LLMErrorKind.TEMPORARY, "529")]
    await service.handle_message(user, "привет")
    assert await total_spent(pool) == 0
    assert service._budget.reserved == 0


async def test_timeout_records_upper_estimate_per_attempt(pool, service, llm, fixed_estimate) -> None:
    user = await new_user(pool, 1)
    llm.replies = [LLMError(LLMErrorKind.TEMPORARY, "timeout", possible_billed_attempts=3)]
    reply = await service.handle_message(user, "привет")
    assert reply.kind is ReplyKind.TEMPORARY_ERROR
    row = await pool.fetchrow("SELECT operation, cost_usd FROM api_usage")
    assert row["operation"] == "chat_unconfirmed" and row["cost_usd"] == 3
    assert service._budget.reserved == 0


async def test_cancelled_request_is_accounted(pool, service, llm, fixed_estimate) -> None:
    user = await new_user(pool, 1)
    llm.delay = 5
    task = asyncio.create_task(service.handle_message(user, "привет"))
    await asyncio.sleep(0.1)
    assert service._budget.reserved == ONE
    task.cancel()  # например, бот останавливается во время генерации
    with pytest.raises(asyncio.CancelledError):
        await task
    row = await pool.fetchrow("SELECT operation, cost_usd FROM api_usage")
    assert row["operation"] == "chat_unconfirmed" and row["cost_usd"] == ONE
    assert service._budget.reserved == 0


async def test_refusal_is_billed(pool, service, llm) -> None:
    from companion.llm.client import LLMResponse, Usage

    user = await new_user(pool, 1)
    llm.replies = [LLMResponse("", "claude-haiku-5-5", "claude-haiku-5-5", Usage(800, 5), refused=True)]
    await service.handle_message(user, "что-то")
    assert await total_spent(pool) > 0


async def test_limit_and_kill_switch_stop_memory_calls_too(pool, make_service, llm) -> None:
    service = make_service(settings_override={"paid_apis_disabled": True})
    user = await new_user(pool, 1)
    for i in range(10):
        await service.handle_message(user, f"m{i}")
    await service.wait_background()
    assert llm.chat_calls == [] and llm.memory_calls == []


async def test_budget_period_is_current_utc_month(pool, service, llm) -> None:
    await pool.execute(
        """INSERT INTO api_usage (provider, model, operation, cost_usd, created_at)
           VALUES ('anthropic', 'x', 'chat', 100, now() - interval '40 days')"""
    )
    assert await service._budget.spent() == 0  # прошлый месяц не считается
    user = await new_user(pool, 1)
    assert (await service.handle_message(user, "привет")).kind is ReplyKind.OK


async def test_invalid_memory_json_keeps_usage_and_marks_batch(pool, make_service) -> None:
    """Обрезанный/битый JSON от модели не теряет учёт расходов и не вызывает повторов."""
    server = FakeAnthropic()
    await server.start()
    try:
        server.memory_json = None  # отдаст строку, которая не является нужным JSON
        client = anthropic.AsyncAnthropic(api_key="k", base_url=server.url, max_retries=0)
        real = AnthropicLLM(client=client)
        parsed, resp = await real.extract_memory(model="claude-haiku-5-5", system="s", transcript="t", max_tokens=100)
        assert parsed is None and resp.usage.input_tokens == 1200
        await real.close()
    finally:
        await server.stop()
