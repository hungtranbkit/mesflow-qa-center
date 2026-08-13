#!/usr/bin/env bash
set -Eeuo pipefail
mkdir -p /data/logs /data/reports
if [[ ! -s /data/config.json ]]; then
  cp /app/config.json /data/config.json
fi
exec python /app/agent.py
