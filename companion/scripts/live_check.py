"""Проверка характера на НАСТОЯЩЕМ Claude API (платно, обычно меньше $0.01 за прогон).

Запуск (ключ берётся из окружения, в командную строку его не вставляйте):
    set -a && . ./.env && set +a
    PYTHONPATH=src python scripts/live_check.py --yes

Показывает ответы персонажа на типовые сценарии и простые автоматические проверки.
Итоговое качество оценивает человек: проверки ловят только грубые ошибки.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from companion import pricing
from companion.config import PROJECT_ROOT
from companion.llm.client import AnthropicLLM, LLMError
from companion.llm.prompts import build_messages, build_system, build_user_context
from companion.persona import load_persona
from companion.repositories.memory import Fact, StoredMessage

NOW = datetime.now(UTC)


@dataclass
class Scenario:
    name: str
    dialog: list[str]  # чередование user/assistant, последняя реплика — пользователя
    facts: list[Fact] = field(default_factory=list)
    summaries: list[str] = field(default_factory=list)
    pause_minutes: int = 1
    must_contain_any: tuple[str, ...] = ()
    must_not_start_with: tuple[str, ...] = ()
    must_not_end_with_question: bool = False
    max_chars: int = 600


SCENARIOS = [
    Scenario("короткое приветствие", ["привет"], max_chars=300),
    Scenario("обычная беседа (без повторного приветствия)",
             ["привет", "Привет! Как день?", "да нормально, только работы много"],
             must_not_start_with=("привет", "здравствуй", "добрый")),
    Scenario("несколько фактов о себе",
             ["меня зовут Саша, я дизайнер, обожаю джаз, а завтра у меня собеседование"],
             must_contain_any=("Саш", "собеседован", "джаз")),
    Scenario("повторяющийся вопрос",
             ["какую музыку ты любишь?", "Джаз, особенно старые записи с треском пластинки.",
              "понятно", "Угу.", "какую музыку ты любишь?"],
             must_contain_any=("уже", "говорил", "джаз", "Джаз")),
    Scenario("«ты настоящая?»", ["слушай, а ты настоящая?"],
             must_contain_any=("AI", "ИИ", "искусствен", "виртуальн", "нейросет")),
    Scenario("ответ без вопроса",
             ["привет", "Привет! Как прошёл день?", "устал", "Что-то случилось на работе?",
              "да нет, просто длинный день"],
             must_not_end_with_question=True),
    Scenario("долгий разговор с памятью",
             ["помнишь, о чём мы говорили?"],
             facts=[Fact(1, "name", "Саша"), Fact(2, "interest", "Любит горы"),
                    Fact(3, "thread", "Собирается в поход на выходных")],
             summaries=["Саша рассказывал, что планирует поход в горы и выбирает палатку."],
             pause_minutes=60 * 30, must_contain_any=("поход", "гор", "палат")),
]


def history_of(s: Scenario) -> list[StoredMessage]:
    msgs = []
    for i, text in enumerate(s.dialog):
        role = "user" if i % 2 == 0 else "assistant"
        msgs.append(StoredMessage(i + 1, role, text, NOW - timedelta(minutes=len(s.dialog) - i)))
    return msgs


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--yes", action="store_true", help="подтвердить платные запросы")
    parser.add_argument("--model", default=os.environ.get("CHAT_MODEL", "claude-haiku-5-5"))
    args = parser.parse_args()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("Не задан ANTHROPIC_API_KEY (загрузите .env в окружение).", file=sys.stderr)
        return 2
    if not args.yes:
        print(f"Будет {len(SCENARIOS)} платных запросов к {args.model} (обычно < $0.01). "
              "Повторите с --yes.")
        return 1

    persona = load_persona(PROJECT_ROOT / "persona" / "mira.toml")
    prompt = persona.system_prompt()
    llm = AnthropicLLM(effort=os.environ.get("LLM_EFFORT", "low"), max_retries=1, timeout=60)
    total = Decimal(0)
    failures = 0
    try:
        await llm.check_model(args.model)
        for s in SCENARIOS:
            hist = history_of(s)
            prev = StoredMessage(0, "assistant", "…", NOW - timedelta(minutes=s.pause_minutes)) \
                if len(hist) == 1 and s.pause_minutes > 1 else (hist[-2] if len(hist) > 1 else None)
            ctx = build_user_context(first_name="Саша", facts=s.facts, summaries=s.summaries,
                                     history=hist, previous_message=prev, now=NOW)
            resp = await llm.chat(model=args.model, system=build_system(prompt, ctx),
                                  messages=build_messages(hist, 16000), max_tokens=1000)
            u = resp.usage
            total += pricing.cost_usd(resp.served_model, input_tokens=u.input_tokens,
                                      output_tokens=u.output_tokens,
                                      cache_read_tokens=u.cache_read_tokens,
                                      cache_write_tokens=u.cache_write_tokens)
            text = resp.text
            problems = []
            if resp.refused:
                problems.append("отказ модели")
            if not text:
                problems.append("пустой ответ")
            if len(text) > s.max_chars:
                problems.append(f"длинно: {len(text)} симв.")
            if s.must_contain_any and not any(w.lower() in text.lower() for w in s.must_contain_any):
                problems.append(f"нет ни одного из {s.must_contain_any}")
            if s.must_not_start_with and text.lower().startswith(s.must_not_start_with):
                problems.append("снова здоровается")
            if s.must_not_end_with_question and text.rstrip().endswith("?"):
                problems.append("закончила вопросом")
            if "здравствуйте" in text.lower():
                problems.append("канцелярит")
            failures += bool(problems)
            mark = "OK " if not problems else "!! "
            print(f"\n{mark}[{s.name}] (вход {u.input_tokens}, кэш {u.cache_read_tokens}/"
                  f"{u.cache_write_tokens}, выход {u.output_tokens})")
            print(f"    Пользователь: {s.dialog[-1]}")
            print(f"    {persona.name}: {text}")
            for p in problems:
                print(f"    -> {p}")
    except LLMError as exc:
        print(f"Ошибка API: {exc}", file=sys.stderr)
        return 2
    finally:
        await llm.close()
    print(f"\nИтого: {len(SCENARIOS) - failures}/{len(SCENARIOS)} без замечаний, "
          f"стоимость ≈ ${total:.4f}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
