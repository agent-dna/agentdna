#!/usr/bin/env sh
# Starts the GitHub MCP server and the AgentDNA MCP Gateway in front of it,
# then runs the automated workload. Both read this project's .env.
set -eu
cd "$(dirname "$0")/.."
python_bin="${PYTHON:-python3}"

"$python_bin" mcp_server.py &
server_pid=$!
"$python_bin" -m agentdna.mcp.gateway &
gateway_pid=$!
trap 'kill "$server_pid" "$gateway_pid" 2>/dev/null || true' EXIT INT TERM

# The gateway initialises its AgentDNA identity before it starts listening.
"$python_bin" scripts/wait_for_gateway.py

"$python_bin" automated.py
