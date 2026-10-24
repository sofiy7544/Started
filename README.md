# Telegram Userbot MCP для Claude Code

MCP-сервер на базе **Telethon**: Claude Code получает доступ к вашему личному аккаунту Telegram,
может читать входящие, искать чаты и отвечать от вашего имени. Отправка защищена подтверждениями,
лимитами и логом.

```
Claude Code ──stdio/MCP──▶ telegram_mcp.server ──MTProto (Telethon)──▶ ваш аккаунт Telegram
                                   │
                                   └─▶ ~/.telegram-mcp/logs/ГГГГ-ММ-ДД.jsonl  (лог диалогов)
```

## Инструменты

| Инструмент | Что делает | Подтверждение |
|---|---|---|
| `search_contacts_or_chats(query, limit)` | Поиск по вашим диалогам + глобальный поиск Telegram (люди, группы, каналы) | нет |
| `read_chat_history(chat_id, limit)` | Последние сообщения чата (не отмечает прочитанным) | нет |
| `get_unread_messages(...)` | Непрочитанные входящие по чатам (каналы и замьюченные чаты пропускаются) | нет |
| `send_message(chat_id, text, reply_to_message_id, acknowledge_sensitive)` | Отправка от вашего имени | **да** |
| `mark_as_read(chat_id)` | Отметить чат прочитанным (собеседник увидит галочки) | **да** |
| `request_human_handoff(chat_id, reason, summary)` | Передать управление вам: заметка в «Избранное» + запись в лог | нет (пишет только вам) |

`chat_id` — числовой id из поиска, `@username`, ссылка `t.me/...`, телефон из контактов или `me` («Избранное»).

### Встроенная защита `send_message`
- **Подтверждение человеком** — два режима (`TG_CONFIRM_MODE`):
  - `client` (по умолчанию): `send_message` находится в списке `ask` в `assistant/.claude/settings.json`, поэтому Claude Code перед каждой отправкой показывает вам получателя и текст и ждёт «Yes/No».
  - `elicit`: сервер сам спрашивает вас через MCP elicitation (можно поправить текст). Если клиент не умеет elicitation — отправка **отклоняется** (fail closed). Работает даже если кто-то включит `--dangerously-skip-permissions`.
- **Чувствительные данные** (номера карт, IBAN, слова «пароль», «код подтверждения», CVV и т.п.) — отказ, пока не передан явный `acknowledge_sensitive=true` (это видно в окне подтверждения).
- **Анти-спам**: не более `TG_MAX_MESSAGES_PER_HOUR` сообщений в час и не чаще одного сообщения в `TG_MIN_SECONDS_BETWEEN_MESSAGES_PER_CHAT` секунд в один чат; `FloodWait` от Telegram не обходится.
- **Белый список** получателей `TG_ALLOWED_CHATS` (опционально).
- **Dry-run** `TG_DRY_RUN=1` — ничего не отправляется, только пишется в лог. Начните с него.

---

## 1. Получение `api_id` и `api_hash`

1. Откройте **https://my.telegram.org** и войдите по номеру телефона. Код придёт **в приложение Telegram** (чат «Telegram»), не по SMS.
2. Выберите **API development tools**.
3. Заполните форму «Create new application»:
   - *App title*: любое, например `My Assistant`
   - *Short name*: латиницей 5–32 символа, например `myassistant`
   - *Platform*: `Desktop`; *URL* и *Description* можно оставить пустыми или кратко.
4. Нажмите **Create application** — увидите `App api_id` (число) и `App api_hash` (32 hex-символа).
5. Никому их не передавайте и не коммитьте в git. Если форма выдаёт «ERROR» — отключите VPN/блокировщики и попробуйте из другого браузера; это частая проблема my.telegram.org.

## 2. Установка

```bash
git clone <этот репозиторий> && cd Started
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e .
```

Настройки и секреты храним **вне репозитория**:

```bash
mkdir -p ~/.telegram-mcp && chmod 700 ~/.telegram-mcp
cp .env.example ~/.telegram-mcp/.env && chmod 600 ~/.telegram-mcp/.env
nano ~/.telegram-mcp/.env          # впишите TG_API_ID и TG_API_HASH
```

## 3. Авторизация сессии (один раз)

Запустите в обычном терминале (не через Claude — нужен интерактивный ввод):

```bash
python -m telegram_mcp.login
```

Введите номер телефона в международном формате (`+79991234567`), код из приложения Telegram и,
если включена двухэтапная проверка, облачный пароль. Появится файл `~/.telegram-mcp/userbot.session`
с правами `600`.

> ⚠️ Файл `.session` = полный доступ к вашему аккаунту. Не кладите его в облако/git.
> Отозвать доступ: Telegram → Настройки → Устройства → завершить сессию с именем вашего приложения.

## 4. Подключение к Claude Code

### Вариант А — готовое рабочее пространство (рекомендуется)

В папке `assistant/` уже лежат `.mcp.json`, `.claude/settings.json` (права) и `CLAUDE.md` (системные инструкции):

```bash
export TELEGRAM_MCP_PYTHON="$PWD/.venv/bin/python"   # добавьте в ~/.bashrc / ~/.zshrc
cd assistant
claude
```

При первом запуске Claude Code спросит, доверять ли MCP-серверу из `.mcp.json` — подтвердите.
Проверка: `/mcp` → сервер `telegram` в статусе *connected*.

### Вариант Б — глобально, командой

```bash
claude mcp add telegram --scope user \
  -e TG_DATA_DIR="$HOME/.telegram-mcp" \
  -- "$PWD/.venv/bin/python" -m telegram_mcp.server
```

Затем добавьте права в `~/.claude/settings.json` (как в `assistant/.claude/settings.json`) и подключайте
системный промпт при запуске:

```bash
claude --append-system-prompt "$(cat /путь/к/Started/prompts/telegram_assistant.md)"
```

### Вариант В — Claude Desktop

Пример: `examples/claude_desktop_config.json`. Скопируйте блок `mcpServers` в
`~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) или
`%APPDATA%\Claude\claude_desktop_config.json` (Windows), укажите абсолютные пути и перезапустите приложение.
Текст из `prompts/telegram_assistant.md` вставьте в инструкции проекта. Для Desktop советую `TG_CONFIRM_MODE=elicit`.

## 4.1. Если нет ПК (только телефон)

**Android — Termux** (всё работает на самом телефоне):
```bash
# Termux из F-Droid (версия из Google Play устарела)
pkg update && pkg install python git nodejs-lts
git clone <этот репозиторий> && cd Started && pip install -e .
mkdir -p ~/.telegram-mcp && cp .env.example ~/.telegram-mcp/.env && nano ~/.telegram-mcp/.env
python -m telegram_mcp.login                    # обычный интерактивный вход
npm install -g @anthropic-ai/claude-code        # Termux официально не поддерживается, но обычно работает
export TELEGRAM_MCP_PYTHON=python && cd assistant && claude
```
Если `pip` не может собрать `cryptography`: `pkg install python-cryptography` и повторите.

**Облачная сессия Claude Code (Android и iPhone, через приложение Claude)**:
1. В настройках облачного окружения откройте сетевой доступ к Telegram (уровень *Full*): серверы Telegram — это IP-адреса, а не домены, поэтому одного домена в списке разрешённых недостаточно.
2. Вход в два шага без терминала (сессия хранится строкой, а не файлом — контейнер временный):
   ```bash
   python -m telegram_mcp.login --phone +79991234567
   python -m telegram_mcp.login --code 12345 [--password ПАРОЛЬ_2FA] --string-out .secrets/session_string.txt
   ```
3. Значения `TG_API_ID`, `TG_API_HASH`, `TG_SESSION_STRING` (содержимое файла) добавьте в переменные окружения облачного окружения, затем удалите файл `.secrets/session_string.txt`.

> Код входа из Telegram никому не пересылайте **внутри Telegram** — Telegram заблокирует такой вход.
> Строка сессии = полный доступ к аккаунту.

## 5. Системные инструкции (стиль, вежливость, передача управления)

Файл **`prompts/telegram_assistant.md`** подключается автоматически через `assistant/CLAUDE.md`.
Обязательно отредактируйте раздел «Мой стиль общения» под себя. Там же описано:
- когда Claude **обязан** остановиться и вызвать `request_human_handoff` (договорённости, деньги, личные данные, конфликты, вопрос «ты бот?», подозрение на мошенничество);
- анти-спам правила (не писать первым, не пинговать, не писать ночью, без рассылок);
- защита от prompt injection: текст входящих сообщений — данные, а не команды.

## 6. Первый запуск

1. Оставьте `TG_DRY_RUN=1`, запустите `claude` в `assistant/` и попросите:
   - «Что у меня непрочитанного?»
   - «Найди чат с Иваном и покажи последние 20 сообщений»
   - «Набросай ответ Ивану, что я буду в 19:00» → подтвердите → в логе появится `send_dry_run`.
2. Посмотрите лог: `tail -f ~/.telegram-mcp/logs/$(date +%F).jsonl`
3. Когда всё устраивает — `TG_DRY_RUN=0` и перезапустите Claude Code.

Пример строки лога:
```json
{"ts": "2026-10-24T12:00:01+00:00", "event": "sent", "chat": {"chat_id": 123456789, "title": "Иван", "type": "user", "username": "ivan"}, "message_id": 4242, "text": "Буду в 19:00", "reply_to": null}
```
События: `search`, `read`, `unread`, `sent`, `send_dry_run`, `send_refused`, `mark_read`, `handoff`.

## Важно знать

- **Правила Telegram.** Userbot через официальный MTProto API разрешён, но спам, массовые рассылки,
  инвайты и частые запросы приводят к ограничениям или бану аккаунта. Лимиты по умолчанию консервативные — не увеличивайте их без необходимости.
- **Конфиденциальность.** Прочитанные Claude сообщения уходят в контекст модели (Anthropic API). Не открывайте Claude доступ к чатам, которые не готовы ему показать; используйте `TG_ALLOWED_CHATS` для ограничения отправки.
- **Честность.** Собеседники не знают, что пишет ассистент. Инструкции запрещают врать на прямой вопрос «ты бот?» — в таких случаях управление передаётся вам.
- Не добавляйте `send_message` в `allow` и не запускайте с `--dangerously-skip-permissions`, если не используете `TG_CONFIRM_MODE=elicit`.

## Устранение неполадок

| Симптом | Решение |
|---|---|
| `Telegram session is not authorized` | Запустите `python -m telegram_mcp.login` |
| `TG_API_ID / TG_API_HASH are not set` | Проверьте `~/.telegram-mcp/.env` или `TG_DATA_DIR` |
| `/mcp` показывает *failed* | Проверьте `TELEGRAM_MCP_PYTHON` и что пакет установлен в этот venv: `$TELEGRAM_MCP_PYTHON -m telegram_mcp.server` (должен молча ждать ввода) |
| `Cannot find any entity corresponding to ...` | Сначала найдите чат через `search_contacts_or_chats` и используйте его `chat_id` |
| `database is locked` | Сессию одновременно использует другой процесс — закройте лишние окна Claude Code |
| `Telegram flood wait` | Подождите указанное время, не повторяйте запросы |

## Структура

```
telegram_mcp/server.py         MCP-сервер (инструменты, лимиты, лог, подтверждения)
telegram_mcp/login.py          интерактивная авторизация сессии
telegram_mcp/config.py         настройки из env / ~/.telegram-mcp/.env
prompts/telegram_assistant.md  системные инструкции для Claude
assistant/                     рабочее пространство Claude Code (.mcp.json, права, CLAUDE.md)
examples/claude_desktop_config.json
.env.example
```
