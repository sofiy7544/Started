@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
echo === Установка персонального ассистента ===
echo.

where uv >nul 2>nul
if errorlevel 1 (
    echo Устанавливаю менеджер пакетов uv ^(официальный установщик astral.sh^)...
    powershell -NoProfile -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex"
    set "PATH=%USERPROFILE%\.local\bin;%PATH%"
)
where uv >nul 2>nul
if errorlevel 1 (
    echo Не удалось установить uv. Закройте окно, откройте install.bat ещё раз.
    pause
    exit /b 1
)

echo Устанавливаю Python 3.12 и зависимости ^(первый раз — пару минут^)...
uv sync --python 3.12
if errorlevel 1 (
    echo Ошибка установки зависимостей. Сообщение выше пришлите ассистенту.
    pause
    exit /b 1
)

if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo.
    echo Создан файл .env — сейчас он откроется в Блокноте.
    echo Впишите BOT_TOKEN, ALLOWED_TELEGRAM_IDS и OPENAI_API_KEY, сохраните и закройте.
    notepad ".env"
)

echo.
echo Готово. Запуск бота — двойной клик по start.bat
pause
