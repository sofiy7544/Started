# Этап 1. Исследование GitHub и выбор основы

Проверено 2026-11-04 по страницам GitHub, документации и PyPI. Звёзды — как показывает
GitHub (округлённо).

## Готовые платформы Telegram-агентов

| Проект | ★ | Лицензия | Активность | Стек | Claude/GPT/Ollama | Подтверждение опасных действий | Вывод |
|---|---|---|---|---|---|---|---|
| [HKUDS/nanobot](https://github.com/HKUDS/nanobot) | 49.8k | MIT | v0.4.0, 28.10.2026 | Python | да/да/да | нет; на Windows без песочницы | лучшая готовая, но без подтверждений |
| [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) | 260.5k | MIT | v0.22.7, 03.11.2026 | — | да/да/— | да (в Telegram) | несколько CVE за 2026, огромная кодовая база |
| [AstrBotDevs/AstrBot](https://github.com/AstrBotDevs/AstrBot) | 46.0k | AGPL-3.0 | v4.29.4, 31.10.2026 | Python | да/да/да | песочница | ориентирован на китайские мессенджеры |
| [langbot-app/LangBot](https://github.com/langbot-app/LangBot) | 18.7k | Apache-2.0 | 04.11.2026 | Python | да/да/да | контроль доступа | для многопользовательских корпоративных ботов |
| [openclaw/openclaw](https://github.com/openclaw/openclaw) | 393.9k | MIT | 03.11.2026 | Node/TS | плагины | песочница выключена | CVE-2026-25253, вредоносные skills, 135k открытых установок |
| [overwirehq/claude-code-telegram](https://github.com/overwirehq/claude-code-telegram) | 2.8k | MIT | 22.09.2026 | Python | только Claude | да | агент для программирования |
| [n3d1117/chatgpt-telegram-bot](https://github.com/n3d1117/chatgpt-telegram-bot) | 3.5k | GPL-2.0 | 12.2024 | Python | только GPT | открыт всем по умолчанию | заброшен |
| [khoj-ai/khoj](https://github.com/khoj-ai/khoj) | 38.0k | AGPL-3.0 | 08.2026 | — | да | — | нет Telegram |
| [letta-ai/letta](https://github.com/letta-ai/letta) | 25.3k | Apache-2.0 | → letta-code (TS) | — | да/да/— | правила | идея памяти, не основа |

Вывод: ни одна платформа не закрывает требования целиком, прежде всего обязательное
подтверждение опасных действий при приемлемой безопасности. Выбрана **комбинация компонентов**
со своим небольшим ядром (решение подтверждено владельцем).

## Компоненты

| Задача | Выбор | Почему | Отклонено |
|---|---|---|---|
| Telegram | aiogram 3 | асинхронный, проверен в проекте companion | — |
| Модели и инструменты | [pydantic-ai](https://github.com/pydantic/pydantic-ai) (21k★, MIT) | единый интерфейс Claude/OpenAI/Ollama, MCP-клиент, подтверждение вызовов, учёт стоимости | LiteLLM — два заражённых релиза на PyPI в 2026 ([Datadog](https://securitylabs.datadoghq.com/articles/litellm-compromised-pypi-docker/)) |
| MCP | [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) v2 | стандарт подключения сервисов | — |
| Память | SQLite + FTS5, позже sqlite-vec | без отдельного сервера, работает на Windows | mem0/Graphiti — завязаны на OpenAI или требуют серверов |
| Речь → текст | gpt-4o-mini-transcribe / [faster-whisper](https://github.com/SYSTRAN/faster-whisper) | облако или бесплатно локально | — |
| Текст → речь | gpt-4o-mini-tts / [edge-tts](https://github.com/rany2/edge-tts) / ElevenLabs | модульно | Silero — некоммерческая лицензия; Kokoro — нет русского |
| Документы | [docling](https://github.com/docling-project/docling) + pandas/openpyxl | OCR сканов, Windows | — |

## Цены (USD за 1M токенов)

Anthropic — [официальная страница](https://platform.claude.com/docs/en/about-claude/pricing):
Haiku 5.5 $0.10/$0.50, Sonnet 5.5 $2/$10, Opus 5.5 $4/$20.
OpenAI — из справочника genai-prices (официальная страница из среды разработки недоступна;
для моделей Anthropic справочник совпал с официальной таблицей): gpt-6-luna $0.10/$0.50,
gpt-6.1-terra $1/$5, gpt-6.1-sol $2/$10.
