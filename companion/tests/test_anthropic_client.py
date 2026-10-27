"""Настоящий Anthropic SDK против локального фейкового сервера: без сети и без расходов."""

from __future__ import annotations

import anthropic
import pytest

from companion.llm.client import AnthropicLLM, LLMError, LLMErrorKind
from tests.fakes import FakeAnthropic

SYSTEM = [{"type": "text", "text": "persona"}]
MESSAGES = [{"role": "user", "content": "привет"}]


@pytest.fixture
async def server():
    s = FakeAnthropic()
    await s.start()
    yield s
    await s.stop()


def make_llm(server: FakeAnthropic, **kw) -> AnthropicLLM:
    client = anthropic.AsyncAnthropic(
        api_key="test-key", base_url=server.url, max_retries=kw.pop("max_retries", 2), timeout=5,
    )
    return AnthropicLLM(client=client, **kw)


async def test_chat_success_returns_text_and_usage(server) -> None:
    llm = make_llm(server)
    resp = await llm.chat(model="claude-haiku-5-5", system=SYSTEM, messages=MESSAGES, max_tokens=500)
    assert resp.text == "привет, рада тебя видеть"
    assert resp.usage.input_tokens == 1200 and resp.usage.output_tokens == 40
    body = server.requests[0]
    assert body["max_tokens"] == 500 and body["output_config"] == {"effort": "low"}
    assert "temperature" not in body
    assert "fallbacks" not in body  # для Haiku серверного fallback нет
    await llm.close()


async def test_invalid_api_key_is_auth_error_without_retries(server) -> None:
    server.statuses = [401] * 5
    llm = make_llm(server)
    with pytest.raises(LLMError) as err:
        await llm.chat(model="claude-haiku-5-5", system=SYSTEM, messages=MESSAGES, max_tokens=100)
    assert err.value.kind is LLMErrorKind.AUTH
    assert len(server.requests) == 1  # 401 не повторяется
    await llm.close()


async def test_overload_is_retried_a_bounded_number_of_times(server) -> None:
    server.statuses = [529] * 10
    llm = make_llm(server, max_retries=2)
    with pytest.raises(LLMError) as err:
        await llm.chat(model="claude-haiku-5-5", system=SYSTEM, messages=MESSAGES, max_tokens=100)
    assert err.value.kind is LLMErrorKind.TEMPORARY
    assert len(server.requests) == 3  # 1 попытка + 2 повтора, не больше
    await llm.close()


async def test_temporary_error_then_success(server) -> None:
    server.statuses = [429]
    llm = make_llm(server)
    resp = await llm.chat(model="claude-haiku-5-5", system=SYSTEM, messages=MESSAGES, max_tokens=100)
    assert resp.text and len(server.requests) == 2
    await llm.close()


async def test_bad_request_is_not_retried(server) -> None:
    server.statuses = [400]
    llm = make_llm(server)
    with pytest.raises(LLMError) as err:
        await llm.chat(model="claude-haiku-5-5", system=SYSTEM, messages=MESSAGES, max_tokens=100)
    assert err.value.kind is LLMErrorKind.BAD_REQUEST and len(server.requests) == 1
    await llm.close()


async def test_unreachable_server_is_temporary() -> None:
    client = anthropic.AsyncAnthropic(api_key="k", base_url="http://127.0.0.1:9", max_retries=0, timeout=2)
    llm = AnthropicLLM(client=client)
    with pytest.raises(LLMError) as err:
        await llm.chat(model="claude-haiku-5-5", system=SYSTEM, messages=MESSAGES, max_tokens=100)
    assert err.value.kind is LLMErrorKind.TEMPORARY
    await llm.close()


async def test_refusal_and_truncation_flags(server) -> None:
    llm = make_llm(server)
    server.stop_reason = "refusal"
    resp = await llm.chat(model="claude-haiku-5-5", system=SYSTEM, messages=MESSAGES, max_tokens=100)
    assert resp.refused and resp.text == ""
    server.stop_reason = "max_tokens"
    resp = await llm.chat(model="claude-haiku-5-5", system=SYSTEM, messages=MESSAGES, max_tokens=100)
    assert resp.truncated and resp.text
    await llm.close()


async def test_sonnet_uses_server_side_fallback(server) -> None:
    llm = make_llm(server)
    await llm.chat(model="claude-sonnet-5-5", system=SYSTEM, messages=MESSAGES, max_tokens=100)
    assert server.requests[0]["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in server.headers[0].get("anthropic-beta", "")
    llm_off = make_llm(server, refusal_fallbacks=False)
    await llm_off.chat(model="claude-sonnet-5-5", system=SYSTEM, messages=MESSAGES, max_tokens=100)
    assert "fallbacks" not in server.requests[1]
    await llm.close()
    await llm_off.close()


async def test_memory_extraction_uses_structured_output(server) -> None:
    server.memory_json = {
        "summary": "Познакомились.", "preferred_name": "Саша",
        "new_facts": [{"category": "interest", "text": "Любит джаз"}], "obsolete_fact_ids": [3],
    }
    llm = make_llm(server)
    parsed, resp = await llm.extract_memory(
        model="claude-haiku-5-5", system="sys", transcript="t", max_tokens=500
    )
    assert parsed.preferred_name == "Саша" and parsed.new_facts[0].text == "Любит джаз"
    assert parsed.obsolete_fact_ids == [3]
    assert server.requests[0]["output_config"]["format"]["type"] == "json_schema"
    assert resp.usage.output_tokens == 40
    await llm.close()
