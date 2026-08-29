#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")/.."
exec python scripts/db-demo-workflow.py reset --confirm "${1:-RESET MESFLOW_DEMO}"
