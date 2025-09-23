#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Config
: "${TELEGRAM_BOT_TOKEN:?TELEGRAM_BOT_TOKEN is required}"
: "${TELEGRAM_CHAT_ID:?TELEGRAM_CHAT_ID is required}"

# Ensure venv
if [ ! -d "$SCRIPT_DIR/.venv" ]; then
  python3 -m venv "$SCRIPT_DIR/.venv"
  "$SCRIPT_DIR/.venv/bin/python" -m pip install --upgrade pip
fi

# Install deps
"$SCRIPT_DIR/.venv/bin/python" -m pip install -r "$SCRIPT_DIR/requirements.txt"

# Run daemon (posts daily at 08:00 CET)
export TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID
exec "$SCRIPT_DIR/.venv/bin/python" "$SCRIPT_DIR/Arxiv_bot.py" --daemon
