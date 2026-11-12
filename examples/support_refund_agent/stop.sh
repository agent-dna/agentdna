#!/usr/bin/env bash
# Stop Kong, the gate service and both agents.

cd "$(dirname "$0")"
for port in 8810 8811 8812; do
  pid=$(lsof -tiTCP:$port -sTCP:LISTEN 2> /dev/null)
  [ -n "$pid" ] && kill $pid
done
docker compose -p support-refund-agent --env-file .env -f kong/docker-compose.yml down > /dev/null 2>&1
echo "Stopped."
