#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")/../.."
python_bin="${PYTHON:-python3}"
"$python_bin" -m mcp_server &
server_pid=$!
trap 'kill "$server_pid" 2>/dev/null || true' EXIT INT TERM
sleep 1
"$python_bin" -m automated