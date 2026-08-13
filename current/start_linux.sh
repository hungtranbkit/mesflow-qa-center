#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
[[ -x .venv/bin/python ]] || { echo "Chưa cài. Chạy: sudo ./install.sh"; exit 1; }
exec .venv/bin/python agent.py
