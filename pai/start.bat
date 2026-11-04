@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYDANTIC_AI_NO_BANNER=1
echo Запускаю ассистента. Остановить — закройте это окно или нажмите Ctrl+C.
echo.
uv run python -m pai
echo.
echo Бот остановлен.
pause
