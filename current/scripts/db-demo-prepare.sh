#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")/.."
exec python scripts/db-demo-workflow.py prepare --confirm "${1:-PREPARE MESFLOW_DEMO}"
