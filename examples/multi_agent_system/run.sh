#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── env check ──────────────────────────────────────────────────────────────
if [ ! -f "$SCRIPT_DIR/.env" ]; then
    echo "ERROR: .env file not found. Run: cp .env.sample .env and fill in your keys."
    exit 1
fi

# ── venv setup (skipped if .venv already exists) ───────────────────────────
echo "Setting up environments (first run may take a few minutes)..."
for dir in karley_agent_adk nate_agent_crewai kaitlynn_agent_langgraph host_agent_adk; do
    echo "  → $dir"
    (cd "$SCRIPT_DIR/$dir" && uv sync --quiet)
done

# ── start sub-agents in background ─────────────────────────────────────────
echo ""
echo "Starting sub-agents..."

(cd "$SCRIPT_DIR/karley_agent_adk" && uv run --no-sync .) &
KARLEY_PID=$!
echo "  ✓ Karley  (port 10002, PID $KARLEY_PID)"

(cd "$SCRIPT_DIR/nate_agent_crewai" && uv run --no-sync .) &
NATE_PID=$!
echo "  ✓ Nate    (port 10003, PID $NATE_PID)"

(cd "$SCRIPT_DIR/kaitlynn_agent_langgraph" && uv run --no-sync -m app.__main__) &
KAITLYNN_PID=$!
echo "  ✓ Kaitlynn (port 10004, PID $KAITLYNN_PID)"

# ── cleanup on exit ────────────────────────────────────────────────────────
cleanup() {
    echo ""
    echo "Shutting down all agents..."
    kill "$KARLEY_PID" "$NATE_PID" "$KAITLYNN_PID" 2>/dev/null || true
    wait "$KARLEY_PID" "$NATE_PID" "$KAITLYNN_PID" 2>/dev/null || true
    exit 0
}
trap cleanup SIGINT SIGTERM

# ── wait for sub-agents to be ready ────────────────────────────────────────
echo ""
echo "Waiting for sub-agents to start..."
sleep 3

# ── start host agent (foreground) ──────────────────────────────────────────
echo "Starting host agent (Streamlit UI)..."
(cd "$SCRIPT_DIR/host_agent_adk" && uv run --no-sync streamlit run webui_app.py)

cleanup
