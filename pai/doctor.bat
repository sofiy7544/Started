@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
uv run python -m pai doctor
pause
