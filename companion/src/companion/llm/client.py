"""Обёртка над Anthropic SDK. Сервисы зависят от протокола LLMClient, тесты подменяют его."""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import anthropic
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

# Модели, для которых доступен серверный fallbacks: "default" (повтор на другой модели при отказе).
_FALLBACK_MODELS = {"claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5", "claude-fable-5-1"}
_FALLBACK_BETA = "server-side-fallback-2026-07-01"


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass(frozen=True)
class LLMResponse:
    text: str
    requested_model: str
    served_model: str
    usage: Usage
    refused: bool = False
    truncated: bool = False


class NewFact(BaseModel):
    category: Literal["interest", "preference", "fact", "thread"] = Field(
        description="interest — увлечения; preference — вкусы и как ему удобнее общаться; "
        "fact — устойчивые сведения о жизни; thread — незавершённая тема, к которой стоит вернуться"
    )
    text: str = Field(description="Одно короткое утверждение в третьем лице")


class MemoryExtraction(BaseModel):
    summary: str = Field(description="Краткое резюме фрагмента разговора, 2-4 предложения")
    preferred_name: str | None = Field(
        default=None,
        description="Как пользователь просит к нему обращаться, если он сказал об этом в этом фрагменте",
    )
    new_facts: list[NewFact] = Field(default_factory=list)
    obsolete_fact_ids: list[int] = Field(
        default_factory=list,
        description="id известных фактов, которые устарели, опровергнуты или (для thread) уже обсуждены",
    )


class LLMErrorKind(enum.Enum):
    AUTH = "auth"  # неверный/отозванный ключ, нет доступа — нужна правка конфигурации
    TEMPORARY = "temporary"  # 429, 5xx, перегрузка, сеть, таймаут — после повторов SDK
    BAD_REQUEST = "bad_request"  # 400/404 — ошибка запроса или имени модели


class LLMError(RuntimeError):
    """Ошибка вызова модели. Текст переписки в сообщение не попадает."""

    def __init__(self, kind: LLMErrorKind, detail: str) -> None:
        super().__init__(f"{kind.value}: {detail}")
        self.kind = kind


class LLMClient(Protocol):
    async def chat(
        self, *, model: str, system: list[dict[str, Any]], messages: list[dict[str, Any]],
        max_tokens: int,
    ) -> LLMResponse: ...

    async def extract_memory(
        self, *, model: str, system: str, transcript: str, max_tokens: int,
    ) -> tuple[MemoryExtraction | None, LLMResponse]: ...


def classify_error(exc: anthropic.APIError) -> LLMError:
    status = getattr(exc, "status_code", None)
    detail = f"{type(exc).__name__} status={status} request_id={getattr(exc, 'request_id', None)}"
    if isinstance(exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
        return LLMError(LLMErrorKind.AUTH, detail)
    if isinstance(exc, (anthropic.RateLimitError, anthropic.APIConnectionError)):
        return LLMError(LLMErrorKind.TEMPORARY, detail)
    if isinstance(exc, anthropic.APIStatusError) and (status or 0) >= 500:
        return LLMError(LLMErrorKind.TEMPORARY, detail)
    if isinstance(exc, anthropic.APIStatusError) and status in (408, 409):
        return LLMError(LLMErrorKind.TEMPORARY, detail)
    return LLMError(LLMErrorKind.BAD_REQUEST, detail)


def _usage(resp: Any) -> Usage:
    u = resp.usage
    return Usage(
        input_tokens=u.input_tokens or 0,
        output_tokens=u.output_tokens or 0,
        cache_read_tokens=getattr(u, "cache_read_input_tokens", None) or 0,
        cache_write_tokens=getattr(u, "cache_creation_input_tokens", None) or 0,
    )


def _text(resp: Any) -> str:
    return "".join(b.text for b in resp.content if b.type == "text").strip()


class AnthropicLLM:
    def __init__(
        self,
        *,
        effort: str = "low",
        refusal_fallbacks: bool = True,
        max_retries: int = 2,
        timeout: float = 45.0,
        client: anthropic.AsyncAnthropic | None = None,
    ) -> None:
        # Ключ SDK берёт из ANTHROPIC_API_KEY. Повторы 408/409/429/5xx/сеть с экспоненциальной
        # задержкой делает сам SDK, ровно max_retries раз — своих циклов повторов нет.
        self._client = client or anthropic.AsyncAnthropic(max_retries=max_retries, timeout=timeout)
        self._effort = effort
        self._fallbacks = refusal_fallbacks

    async def close(self) -> None:
        await self._client.close()

    async def chat(
        self, *, model: str, system: list[dict[str, Any]], messages: list[dict[str, Any]],
        max_tokens: int,
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
            "output_config": {"effort": self._effort},
        }
        try:
            if self._fallbacks and model in _FALLBACK_MODELS:
                resp = await self._client.beta.messages.create(
                    **kwargs, betas=[_FALLBACK_BETA], fallbacks="default"
                )
            else:
                resp = await self._client.messages.create(**kwargs)
        except anthropic.APIError as exc:
            raise classify_error(exc) from exc

        refused = resp.stop_reason == "refusal"
        if refused:
            category = getattr(getattr(resp, "stop_details", None), "category", None)
            log.warning("model refusal: model=%s category=%s", resp.model, category)
        return LLMResponse(
            text="" if refused else _text(resp),
            requested_model=model,
            served_model=resp.model or model,
            usage=_usage(resp),
            refused=refused,
            truncated=resp.stop_reason == "max_tokens",
        )

    async def extract_memory(
        self, *, model: str, system: str, transcript: str, max_tokens: int,
    ) -> tuple[MemoryExtraction | None, LLMResponse]:
        try:
            resp = await self._client.messages.parse(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": transcript}],
                output_format=MemoryExtraction,
                output_config={"effort": "low"},
            )
        except anthropic.APIError as exc:
            raise classify_error(exc) from exc

        ok = resp.stop_reason not in ("refusal", "max_tokens")
        return (resp.parsed_output if ok else None), LLMResponse(
            text="", requested_model=model, served_model=resp.model or model,
            usage=_usage(resp), refused=resp.stop_reason == "refusal",
            truncated=resp.stop_reason == "max_tokens",
        )
