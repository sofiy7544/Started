# Telegram-ассистент (облачная сессия / корень репозитория)

MCP-сервер `telegram` подключается из `.mcp.json` (запуск `python3 -m telegram_mcp.server` из корня).
Зависимости ставит setup script окружения: `pip install "telethon>=1.36,<2" "mcp>=1.12,<2" python-dotenv`.
Секреты (`TG_API_ID`, `TG_API_HASH`, `TG_SESSION_STRING`) — только в переменных окружения, не в чате и не в git.

@prompts/telegram_assistant.md
