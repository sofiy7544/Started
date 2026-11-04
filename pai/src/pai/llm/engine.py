"""Единый интерфейс к моделям любых провайдеров (через pydantic-ai)."""

from __future__ import annotations

import enum
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from pydantic_ai import Agent, ModelMessage, ModelMessagesTypeAdapter, UsageLimits
from pydantic_ai import exceptions as ai_exc
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings

from pai.config import Settings
from pai.llm.catalog import ModelInfo

log = logging.getLogger(__name__)


class FailureKind(enum.Enum):
    AUTH = "auth"  # ключ неверный или отозван
    QUOTA = "quota"  # у провайдера закончились деньги/квота
    RATE_LIMIT = "rate_limit"  # слишком много запросов
    TEMPORARY = "temporary"  # перегрузка, 5xx, сеть
    TIMEOUT = "timeout"  # ответа не дождались — запрос мог быть оплачен
    MODEL_NOT_FOUND = "model_not_found"
    CONTEXT_TOO_LONG = "context_too_long"
    TOO_MANY_STEPS = "too_many_steps"
    BAD_RESPONSE = "bad_response"
    REFUSED = "refused"  # фильтр безопасности провайдера отклонил запрос
    CONFIG = "config"  # ошибка настройки (нет ключа, неверное имя модели в каталоге)


_POSSIBLY_BILLED = frozenset({
    FailureKind.TIMEOUT, FailureKind.TOO_MANY_STEPS, FailureKind.BAD_RESPONSE, FailureKind.REFUSED,
})


class LLMFailure(RuntimeError):
    """Ошибка модели в виде, понятном остальному приложению. Без текста переписки."""

    def __init__(self, kind: FailureKind, detail: str) -> None:
        super().__init__(f"{kind.value}: {detail}")
        self.kind = kind

    @property
    def possibly_billed(self) -> bool:
        """Провайдер мог успеть сгенерировать (и выставить счёт) — ответ не дошёл,
        оказался некорректным, отфильтрован или превысил лимит шагов."""
        return self.kind in _POSSIBLY_BILLED


def classify(exc: Exception) -> LLMFailure:
    detail = type(exc).__name__
    status = getattr(exc, "status_code", None)
    if status:
        detail += f" status={status}"
    # Порядок важен: подклассы раньше родителей.
    if isinstance(exc, ai_exc.UserError):
        return LLMFailure(FailureKind.CONFIG, detail)
    mapping: list[tuple[type[Exception], FailureKind]] = [
        (ai_exc.ContentFilterError, FailureKind.REFUSED),
        (ai_exc.ModelAuthenticationError, FailureKind.AUTH),
        (ai_exc.ModelNotFoundError, FailureKind.MODEL_NOT_FOUND),
        (ai_exc.ModelContextWindowExceededError, FailureKind.CONTEXT_TOO_LONG),
        (ai_exc.ModelQuotaExceededError, FailureKind.QUOTA),
        (ai_exc.ModelRateLimitError, FailureKind.RATE_LIMIT),
        (ai_exc.ModelTimeoutError, FailureKind.TIMEOUT),
        (ai_exc.ModelUnavailableError, FailureKind.TEMPORARY),
        (ai_exc.ModelServerError, FailureKind.TEMPORARY),
        (ai_exc.ModelConnectionError, FailureKind.TEMPORARY),
        (ai_exc.UsageLimitExceeded, FailureKind.TOO_MANY_STEPS),
        (ai_exc.ModelRequestRejectedError, FailureKind.BAD_RESPONSE),
        (ai_exc.UnexpectedModelBehavior, FailureKind.BAD_RESPONSE),
    ]
    for cls, kind in mapping:
        if isinstance(exc, cls):
            return LLMFailure(kind, detail)
    if isinstance(exc, ai_exc.ModelHTTPError):
        if status in (401, 403):
            return LLMFailure(FailureKind.AUTH, detail)
        if status == 429:
            return LLMFailure(FailureKind.RATE_LIMIT, detail)
        if status and status >= 500:
            return LLMFailure(FailureKind.TEMPORARY, detail)
    if isinstance(exc, ai_exc.ModelAPIError):
        return LLMFailure(FailureKind.TEMPORARY, detail)
    return LLMFailure(FailureKind.BAD_RESPONSE, detail)


@dataclass(frozen=True)
class Reply:
    text: str
    new_messages: bytes  # JSON новых сообщений pydantic-ai этого обмена
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cost_usd: Decimal
    requests: int


class ChatEngine(Protocol):
    async def reply(
        self, model: ModelInfo, *, prompt: str, history: list[ModelMessage], instructions: str,
        max_tokens: int, cost_limit_usd: Decimal,
    ) -> Reply: ...


def _export_keys(settings: Settings) -> None:
    """pydantic-ai читает ключи провайдеров из стандартных переменных окружения."""
    if settings.openai_api_key:
        os.environ.setdefault("OPENAI_API_KEY", settings.openai_api_key.get_secret_value())
    if settings.anthropic_api_key:
        os.environ.setdefault("ANTHROPIC_API_KEY", settings.anthropic_api_key.get_secret_value())
    if settings.ollama_base_url:
        os.environ.setdefault("OLLAMA_BASE_URL", settings.ollama_base_url)
    os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")


class PydanticAIEngine:
    """Реальные вызовы моделей. Повторы при временных сбоях делает SDK провайдера,
    ровно llm_max_retries раз; своих циклов повторов нет."""

    def __init__(
        self, settings: Settings, model_factory: Callable[[ModelInfo], Model] | None = None
    ) -> None:
        _export_keys(settings)
        self._settings = settings
        # В тестах сюда передаётся FunctionModel — весь путь pydantic-ai проверяется без API.
        self._model_factory = model_factory
        self._agents: dict[str, Agent[None, str]] = {}

    def _agent(self, model: ModelInfo) -> Agent[None, str]:
        agent = self._agents.get(model.id)
        if agent is None:
            target = self._model_factory(model) if self._model_factory else model.api_model
            agent = Agent(target, output_type=str, defer_model_check=True)
            self._agents[model.id] = agent
        return agent

    async def reply(
        self, model: ModelInfo, *, prompt: str, history: list[ModelMessage], instructions: str,
        max_tokens: int, cost_limit_usd: Decimal,
    ) -> Reply:
        settings = ModelSettings(
            max_tokens=max_tokens,
            timeout=self._settings.llm_timeout_seconds,
            max_retries=self._settings.llm_max_retries,
        )
        # cost_limit pydantic-ai работает только для моделей из справочника цен genai-prices;
        # для остальных (локальные, с ручной ценой) бюджет защищает наш резерв BudgetGuard.
        use_cost_limit = model.price_in is None and model.price_known()
        limits = UsageLimits(request_limit=5, cost_limit=cost_limit_usd if use_cost_limit else None)
        try:
            result = await self._agent(model).run(
                prompt, message_history=history, instructions=instructions,
                model_settings=settings, usage_limits=limits,
            )
        except (ai_exc.AgentRunError, ai_exc.UserError) as exc:
            raise classify(exc) from exc
        return to_reply(model, result)


def to_reply(model: ModelInfo, result) -> Reply:  # type: ignore[no-untyped-def]
    u = result.usage
    cost = u.cost
    if cost is None:
        cost = model.cost_usd(
            input_tokens=u.input_tokens, output_tokens=u.output_tokens,
            cache_read_tokens=u.cache_read_tokens, cache_write_tokens=u.cache_write_tokens,
        )
    if model.price_in is not None and model.price_out is not None:
        # Ручная цена из каталога важнее справочника (например, 0 для локальной модели).
        cost = model.cost_usd(input_tokens=u.input_tokens, output_tokens=u.output_tokens)
    return Reply(
        text=(result.output or "").strip(),
        new_messages=result.new_messages_json(),
        input_tokens=u.input_tokens,
        output_tokens=u.output_tokens,
        cache_read_tokens=u.cache_read_tokens,
        cost_usd=Decimal(str(cost)).quantize(Decimal("0.000001")),
        requests=u.requests,
    )


def load_history(blobs: list[bytes]) -> list[ModelMessage]:
    messages: list[ModelMessage] = []
    for blob in blobs:
        messages.extend(ModelMessagesTypeAdapter.validate_json(blob))
    return messages
