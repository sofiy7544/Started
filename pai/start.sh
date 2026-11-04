#!/usr/bin/env bash
# Запуск на Linux/macOS (или VPS). Первый раз: uv sync
set -euo pipefail
cd "$(dirname "$0")"
export PYDANTIC_AI_NO_BANNER=1
exec uv run python -m pai
