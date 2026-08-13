#!/usr/bin/env bash
set -u
SERVICE=mesflow-qa-center
printf '\n=== SERVICE ===\n'
systemctl status "$SERVICE" --no-pager -l || true
printf '\n=== JOURNAL ===\n'
journalctl -u "$SERVICE" -n 120 --no-pager -l || true
printf '\n=== PORT 8095 ===\n'
ss -ltnp 2>/dev/null | grep -E '[:.]8095[[:space:]]' || echo 'Cổng 8095 đang trống'
printf '\n=== PYTHON PREFLIGHT ===\n'
if [[ -x /opt/mesflow-qa-center/.venv/bin/python ]]; then
  cd /opt/mesflow-qa-center || exit 1
  sudo -u "$(stat -c %U /opt/mesflow-qa-center)" /opt/mesflow-qa-center/.venv/bin/python -c "import flask, requests, psutil; import agent; print('Python preflight OK')" || true
else
  echo 'Không tìm thấy virtualenv tại /opt/mesflow-qa-center/.venv'
fi
