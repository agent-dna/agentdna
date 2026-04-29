#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ ! -f "$SCRIPT_DIR/.env" ]; then
    echo "ERROR: .env not found. Run: cp .env.sample .env and fill in your API key."
    exit 1
fi

echo "Setting up environment..."
(cd "$SCRIPT_DIR" && uv sync --quiet)

echo "Starting Document Analysis Pipeline..."
(cd "$SCRIPT_DIR" && uv run --no-sync streamlit run app.py)
