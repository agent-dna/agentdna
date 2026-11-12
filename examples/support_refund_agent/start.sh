#!/usr/bin/env bash
# Start Kong, the gate service and both agents. Logs go to logs/.
#
#   ./start.sh
#   python support_lead.py          (or: python support_lead.py 4822)
#   ./stop.sh
#
# Docker must be running (for Kong). Use the Python of your virtual environment.

cd "$(dirname "$0")"
mkdir -p logs
PY=${PY:-python}

# 1. Something left running would keep its port, and you would test old code
for port in 8810 8811 8812 8880; do
  if nc -z 127.0.0.1 $port 2> /dev/null; then
    echo "Something is already running on :$port. Run ./stop.sh first."
    exit 1
  fi
done

# 2. Kong, with the AgentDNA plugin and env-key (the apps' keys come from .env)
echo "Kong            :8880"
docker compose -p support-refund-agent --env-file .env -f kong/docker-compose.yml up -d > logs/kong.log 2>&1

# 3. The gate service and the two agents
echo "gate service    :8810"
nohup $PY -u gate_service.py > logs/gate_service.log 2>&1 &
echo "Support Agent   :8811"
nohup $PY -u support_agent.py > logs/support_agent.log 2>&1 &
echo "Payments Agent  :8812"
nohup $PY -u payments_agent.py > logs/payments_agent.log 2>&1 &

# 4. Wait until every port answers
for port in 8810 8811 8812 8880; do
  for second in $(seq 1 60); do
    nc -z 127.0.0.1 $port 2> /dev/null && break
    sleep 1
  done
  nc -z 127.0.0.1 $port 2> /dev/null || { echo ":$port did not start. See logs/."; exit 1; }
done
echo
echo "Ready. Now:  python support_lead.py"
