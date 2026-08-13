#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")/../.."
exec "${PYTHON:-python3}" -m sqlite_analytics_agent.automated