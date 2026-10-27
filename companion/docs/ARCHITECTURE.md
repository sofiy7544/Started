# Virtual Companion Telegram — архитектура

Состояние: **текстовый MVP реализован** (этапы 1–3). Голос, медиа, платежи и
инициативные сообщения — следующие этапы, в коде их нет.

## 1. Обзор

```
 Telegram ──long polling──▶ aiogram Dispatcher
                              │  DedupMiddleware (update_id → processed_updates)
                              │  errors → on_error (лог без текста, ответ «ошибка»)
             ┌────────────────┴───────────────┐
             ▼                                ▼
     handlers/commands.py              handlers/chat.py
     /start /help /privacy             текст → ChatService (+ «печатает…»)
     /reset /memory /forget            не текст → «только текст»
             │                                │
             └──────────────┬─────────────────┘
                            ▼
                      ChatService ──▶ RateLimiter, дневной лимит (БД)
                            │     ──▶ CircuitBreaker (пауза после ошибок API)
                            │     ──▶ BudgetGuard (месячный бюджет, api_usage)
                            ▼
              MemoryService (резюме + факты, фон)    AnthropicLLM (SDK, повторы SDK)
                            │                              │
                            ▼                              ▼
                       PostgreSQL                     Claude API
```

Принципы:

* **Сервисный слой не зависит от Telegram.** `ChatService` принимает пользователя и
  текст и возвращает `ChatReply`. Голос, медиа и платежи позже подключаются как
  новые сервисы и обработчики, без переписывания переписки.
* **Все запросы к пользовательским данным содержат `WHERE user_id = $1`.** Удаление
  фактов «по совету модели» дополнительно ограничено id, которые модели показали.
* **Секреты только из окружения** (`Settings.from_env`), в коде и git их нет.
* **Деньги резервируются до вызова** (атомарно, с учётом параллельных запросов) и
  списываются по фактическому `usage`; таймаут и отмена — по верхней оценке.
* **Логи без текста переписки**: id пользователя, модель, токены, стоимость, тип ошибки.
  `SafeFormatter` печатает в трассировках только типы исключений и места в коде, а не их
  сообщения: в них могут оказаться пользовательские данные (DETAIL у asyncpg, input_value у pydantic).

## 2. Модель и расходы

| Задача | Модель по умолчанию | Настройка |
|---|---|---|
| Ответы персонажа | `claude-haiku-5-5`, effort `low` | `CHAT_MODEL`, `LLM_EFFORT` |
| Резюме и факты (фон) | `claude-haiku-5-5`, structured output | `MEMORY_MODEL` |
| Расход > 80% бюджета | переход на `MEMORY_MODEL` | `BUDGET_SOFT_RATIO` |
| Расход ≥ 100% или `PAID_APIS_DISABLED=1` | вызовов нет | `ANTHROPIC_MONTHLY_BUDGET_USD` |

Если поставить `CHAT_MODEL=claude-sonnet-5-5`, для неё автоматически включается
серверный `fallbacks: "default"` (при отказе классификатора запрос повторяется на
другой модели; выключается `LLM_REFUSAL_FALLBACKS=0`).

Цены и идентификаторы сверены с документацией Anthropic 2026-10-27 (см. README, раздел 6);
при старте ключ и модели проверяются бесплатным Models API.

Ограничители: `MAX_REPLY_TOKENS` (1000), `MAX_CONTEXT_MESSAGES` (40),
`MAX_CONTEXT_CHARS` (16000), `MAX_USER_MESSAGE_CHARS` (2000), `MESSAGES_PER_MINUTE` (8),
`DAILY_MESSAGES_PER_USER` (80), повторы SDK `LLM_MAX_RETRIES` (2, только 408/409/429/5xx/сеть),
предохранитель: 3 ошибки подряд → пауза 60 с, неверный ключ → пауза 10 мин.
Пачка сообщений, присланных, пока персонаж «печатает», получает один ответ — без
лишних вызовов. Промпт персонажа помечен `cache_control` (общий кэш для всех).

## 3. Память

1. **Текущий диалог** — несжатые сообщения после границы `/reset`
   (`users.context_from_message_id`), не больше `MAX_CONTEXT_MESSAGES`/`MAX_CONTEXT_CHARS`.
2. **Резюме** — когда несжатых сообщений ≥ `SUMMARIZE_THRESHOLD` (30), всё, кроме
   последних `KEEP_RECENT_MESSAGES` (14), отправляется дешёвой модели. Результат:
   резюме (`conversation_summaries`, в контекст идут 4 последних) и изменения фактов.
   После `/reset` прежний диалог сжимается сразу.
3. **Факты** (`memory_facts.category`): `name` — как обращаться (одно значение,
   новое заменяет старое), `interest`, `preference`, `fact`, `thread` — незавершённые
   темы (не больше `MAX_OPEN_THREADS`). Противоречия: модель возвращает
   `obsolete_fact_ids`, старые версии удаляются. Лимит `MAX_FACTS_PER_USER`,
   вытесняются давно не подтверждавшиеся.

Защита данных: номера карт (с проверкой Луна), телефоны, e-mail, коды, пароли с
пометкой, паспорт маскируются **до сохранения** сообщения; факты с такими данными и
с «чувствительными» словами отбрасываются; экстрактору запрещено сохранять здоровье,
финансы, документы, адреса и т. п. Сжатые сообщения удаляются через
`MESSAGE_RETENTION_DAYS`.

Параллельность: ответы одному пользователю сериализуются `asyncio.Lock`;
суммаризация держит `pg_try_advisory_xact_lock(ns, user_id)` на время вызова модели,
а `/forget` и полное удаление берут ту же блокировку (блокирующе) и ждут её — иначе
резюме и факты удаляемой переписки записались бы уже после удаления. Ответ хранит
`reply_to_id`, поэтому в истории он стоит сразу за своим вопросом.

Срок хранения: фоновая задача раз в 6 часов удаляет сообщения старше
`MESSAGE_RETENTION_DAYS` у всех пользователей, включая неактивных (`services/maintenance.py`).
Резюме и факты хранятся до `/forget`; в контекст идут только 4 последних резюме.

## 4. Структура каталогов

```
companion/
├── persona/mira.toml          характер, биография, речь (меняется без кода)
├── migrations/                схема БД: 001_init.sql, 002_retention.sql (автоматически при старте)
├── scripts/live_check.py      ручная проверка характера на настоящем API (платно, < $0.01)
├── src/companion/
│   ├── __main__.py            запуск: python -m companion
│   ├── config.py              Settings из переменных окружения
│   ├── persona.py             загрузка TOML + неотключаемые правила (CORE_RULES)
│   ├── texts.py               тексты интерфейса
│   ├── pricing.py             цены моделей → стоимость
│   ├── db.py                  пул asyncpg, миграции
│   ├── repositories/          SQL: users, memory, usage
│   ├── llm/client.py          AnthropicLLM, классификация ошибок, схема памяти
│   ├── llm/prompts.py         system-блоки, контекст пользователя, обрезка истории
│   ├── services/chat.py       ChatService — сценарий ответа
│   ├── services/memory.py     резюме и факты
│   ├── services/budget.py     BudgetGuard
│   ├── services/limits.py     RateLimiter, CircuitBreaker
│   ├── services/sensitive.py  маскирование данных
│   ├── services/maintenance.py срок хранения переписки
│   ├── logging_setup.py       логи без текста исключений
│   └── bot/                   aiogram: app.py, handlers/commands.py, handlers/chat.py
├── tests/                     pytest на реальном PostgreSQL + фейковые API
├── Dockerfile, docker-compose.yml, .env.example
```

## 5. Схема базы данных (001_init.sql + 002_retention.sql)

```
users(id, tg_user_id UNIQUE, first_name, language_code, age_confirmed_at,
      context_from_message_id, created_at, last_active_at)
messages(id, user_id → users CASCADE, role user|assistant, content (маскированный),
         model, reply_to_id, summarized, created_at)
conversation_summaries(id, user_id → users CASCADE, content, first/last_message_id, created_at)
memory_facts(id, user_id → users CASCADE, category, fact, fact_norm,
             UNIQUE(user_id, fact_norm), created_at, updated_at)
api_usage(id, provider, model, operation chat|memory|*_unconfirmed, user_id → users SET NULL,
          input/output/cache_read/cache_write tokens, cost_usd, created_at)
processed_updates(update_id PK, created_at)   — защита от повторной обработки
```

Удаление пользователя (`/forget` → «Все мои данные») удаляет строку `users`, всё
остальное — каскадом; в `api_usage` остаются обезличенные расходы.

## 6. Следующие этапы (план, не реализовано)

* **Голос:** `services/voice.py` (ElevenLabs, один voice_id, кэш по хэшу текста),
  `provider='elevenlabs'` в `api_usage` и отдельный бюджет.
* **Изображения и кружочки:** таблица `media_assets` (только оригинальные/лицензированные
  файлы с полем `license`), кэш `tg_file_id`, подбор по тегам, без генерации на лету.
* **Подписки (Telegram Stars):** таблицы `payments` (UNIQUE `telegram_payment_charge_id`
  для идемпотентности) и `entitlements`; права вычисляются из БД, а не из памяти
  процесса; `/terms`, `/paysupport`; тариф влияет на `CHAT_MODEL` и лимиты.
* **Инициативные сообщения:** только opt-in сценарии, тихие часы, команда отключения.
* **Эксплуатация:** резервные копии `pg_dump` по расписанию, алерты администратору.
