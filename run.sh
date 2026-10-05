#!/usr/bin/env bash
# YominPost dev launcher: creates .venv on first run, installs the package in editable mode, starts the app.
# Zero-install alternative: uvx --from git+https://github.com/mrlong0129/yominpost yominpost --open
set -e
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "Creating .venv and installing dependencies…"
  if command -v uv >/dev/null 2>&1; then
    uv venv -q .venv && uv pip install -q -p .venv -e ".[dev]"
  else
    python3 -m venv .venv && .venv/bin/pip install -q -e ".[dev]"
  fi
fi

PORT="${PORT:-8300}"
echo "YominPost → http://127.0.0.1:${PORT}  (offline template driver by default; see .env.example)"
exec .venv/bin/python -m uvicorn yominpost.web.app:app --host 127.0.0.1 --port "${PORT}"
