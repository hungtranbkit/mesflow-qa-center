#!/usr/bin/env bash
set -Eeuo pipefail

APP_NAME="mesflow-qa-center"
APP_VERSION="1.20.0"
INSTALL_DIR="${MESFLOW_QA_INSTALL_DIR:-/opt/mesflow-qa-center}"
SERVICE_FILE="/etc/systemd/system/${APP_NAME}.service"
SERVICE_DROPIN_DIR="/etc/systemd/system/${APP_NAME}.service.d"
RUN_USER="${MESFLOW_QA_USER:-${SUDO_USER:-$USER}}"
RUN_GROUP="$(id -gn "$RUN_USER")"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_BROWSER="${MESFLOW_QA_INSTALL_BROWSER:-1}"
QA_HOST="${MESFLOW_QA_HOST:-0.0.0.0}"
QA_PORT="${MESFLOW_QA_PORT:-8095}"
CONFIG_BACKUP=""

log() { printf '\n[QA CLEAN INSTALL V%s] %s\n' "$APP_VERSION" "$*"; }
fail() { printf '\n[QA CLEAN INSTALL V%s] ERROR: %s\n' "$APP_VERSION" "$*" >&2; exit 1; }
cleanup_tmp() { [[ -n "${CONFIG_BACKUP:-}" && -f "$CONFIG_BACKUP" ]] && rm -f "$CONFIG_BACKUP" || true; }
trap cleanup_tmp EXIT

[[ "$(id -u)" -eq 0 ]] || fail "Hãy chạy bằng: sudo ./install.sh"
command -v systemctl >/dev/null 2>&1 || fail "Máy Linux này không dùng systemd."
id "$RUN_USER" >/dev/null 2>&1 || fail "Không tìm thấy user: $RUN_USER"
[[ -f "$SOURCE_DIR/VERSION" ]] || fail "Gói cài thiếu file VERSION"
[[ "$(tr -d '[:space:]' < "$SOURCE_DIR/VERSION")" == "$APP_VERSION" ]] || fail "Version gói cài không khớp"

log "Cài các gói hệ thống"
if command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y python3 python3-venv python3-pip ca-certificates curl rsync procps iproute2
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y python3 python3-pip rsync ca-certificates curl procps-ng iproute
elif command -v yum >/dev/null 2>&1; then
  yum install -y python3 python3-pip rsync ca-certificates curl procps-ng iproute
else
  fail "Chưa hỗ trợ package manager của hệ điều hành này."
fi

log "Sao lưu cấu hình hiện tại"
if [[ -f "$INSTALL_DIR/config.json" ]]; then
  CONFIG_BACKUP="$(mktemp)"
  cp -a "$INSTALL_DIR/config.json" "$CONFIG_BACKUP"
  echo "Đã sao lưu config.json"
else
  echo "Không có config cũ; dùng cấu hình mặc định của gói."
fi

log "Dừng và xóa toàn bộ runtime/code cũ"
systemctl disable --now "$APP_NAME" 2>/dev/null || true
systemctl kill --kill-who=all "$APP_NAME" 2>/dev/null || true
pkill -TERM -f "$INSTALL_DIR/agent.py" 2>/dev/null || true
sleep 1
pkill -KILL -f "$INSTALL_DIR/agent.py" 2>/dev/null || true
rm -f "$SERVICE_FILE"
rm -rf "$SERVICE_DROPIN_DIR"
rm -f /usr/local/bin/mesflow-qa-center /usr/local/bin/mesflow-qa-browser-install /usr/local/bin/mesflow-qa-uninstall
rm -rf "$INSTALL_DIR"
systemctl daemon-reload
systemctl reset-failed "$APP_NAME" 2>/dev/null || true

log "Kiểm tra cổng $QA_PORT sau khi dọn bản cũ"
if ss -ltnp 2>/dev/null | grep -qE "[:.]${QA_PORT}[[:space:]]"; then
  echo "Cổng $QA_PORT vẫn đang do tiến trình khác giữ:" >&2
  ss -ltnp 2>/dev/null | grep -E "[:.]${QA_PORT}[[:space:]]" >&2 || true
  if command -v docker >/dev/null 2>&1; then
    docker ps --format 'table {{.ID}}\t{{.Names}}\t{{.Ports}}' 2>/dev/null | grep "$QA_PORT" >&2 || true
  fi
  fail "Không tự dừng tiến trình không thuộc QA Center. Hãy giải phóng cổng hoặc cài với MESFLOW_QA_PORT=8096."
fi

log "Chép sạch source V$APP_VERSION vào $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
rsync -a --delete \
  --exclude '.venv' --exclude '__pycache__' --exclude '.pytest_cache' \
  --exclude 'reports' --exclude 'logs' --exclude 'backups' \
  "$SOURCE_DIR/" "$INSTALL_DIR/"
mkdir -p "$INSTALL_DIR"/{reports,logs,backups}
if [[ -n "$CONFIG_BACKUP" && -f "$CONFIG_BACKUP" ]]; then
  cp -a "$CONFIG_BACKUP" "$INSTALL_DIR/config.json"
fi
chown -R "$RUN_USER:$RUN_GROUP" "$INSTALL_DIR"

log "Tạo mới hoàn toàn Python virtualenv"
rm -rf "$INSTALL_DIR/.venv"
runuser -u "$RUN_USER" -- python3 -m venv "$INSTALL_DIR/.venv"
runuser -u "$RUN_USER" -- "$INSTALL_DIR/.venv/bin/python" -m pip install --upgrade pip wheel
runuser -u "$RUN_USER" -- "$INSTALL_DIR/.venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"

if [[ "$INSTALL_BROWSER" == "1" ]]; then
  log "Cài Chromium cho Browser Visual Test"
  "$INSTALL_DIR/.venv/bin/python" -m playwright install-deps chromium || log "Không cài được dependency Chromium; API test vẫn dùng được."
  runuser -u "$RUN_USER" -- "$INSTALL_DIR/.venv/bin/python" -m playwright install chromium || log "Không tải được Chromium; có thể cài sau."
fi

log "Python preflight và xác nhận version source"
runuser -u "$RUN_USER" -- "$INSTALL_DIR/.venv/bin/python" - <<PY
import agent
assert agent.APP_VERSION == "$APP_VERSION", (agent.APP_VERSION, "$APP_VERSION")
print("Python preflight OK - version", agent.APP_VERSION)
PY

log "Tạo systemd service V$APP_VERSION"
cat > "$SERVICE_FILE" <<SERVICE
[Unit]
Description=MESFlow QA Center V$APP_VERSION
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
Group=$RUN_GROUP
WorkingDirectory=$INSTALL_DIR
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONUTF8=1
Environment=MESFLOW_QA_HOST=$QA_HOST
Environment=MESFLOW_QA_PORT=$QA_PORT
ExecStart=$INSTALL_DIR/.venv/bin/python $INSTALL_DIR/agent.py
Restart=on-failure
RestartSec=5
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
SERVICE

cat > /usr/local/bin/mesflow-qa-center <<EOF
#!/usr/bin/env bash
set -e
case "\${1:-status}" in
  start|stop|restart|status) exec systemctl "\${1:-status}" $APP_NAME ;;
  logs) exec journalctl -u $APP_NAME -f ;;
  version) exec curl -fsS http://127.0.0.1:$QA_PORT/api/version ;;
  open) printf '%s\n' 'http://127.0.0.1:$QA_PORT' ;;
  *) echo 'Dùng: mesflow-qa-center {start|stop|restart|status|logs|version|open}'; exit 2 ;;
esac
EOF
chmod +x /usr/local/bin/mesflow-qa-center

cat > /usr/local/bin/mesflow-qa-browser-install <<EOF
#!/usr/bin/env bash
set -e
$INSTALL_DIR/.venv/bin/python -m playwright install-deps chromium
runuser -u $RUN_USER -- $INSTALL_DIR/.venv/bin/python -m playwright install chromium
EOF
chmod +x /usr/local/bin/mesflow-qa-browser-install

cat > /usr/local/bin/mesflow-qa-uninstall <<EOF
#!/usr/bin/env bash
set -e
[[ "\$(id -u)" -eq 0 ]] || { echo 'Hãy chạy: sudo mesflow-qa-uninstall'; exit 1; }
systemctl disable --now $APP_NAME 2>/dev/null || true
rm -f $SERVICE_FILE
rm -rf $SERVICE_DROPIN_DIR
rm -f /usr/local/bin/mesflow-qa-center /usr/local/bin/mesflow-qa-browser-install /usr/local/bin/mesflow-qa-uninstall
systemctl daemon-reload
printf 'Đã gỡ service. Muốn xóa dữ liệu: sudo rm -rf %s\n' '$INSTALL_DIR'
EOF
chmod +x /usr/local/bin/mesflow-qa-uninstall

systemctl daemon-reload
systemctl enable "$APP_NAME"
systemctl restart "$APP_NAME"

log "Chờ service và xác nhận đúng V$APP_VERSION"
READY=0
for _ in $(seq 1 20); do
  if curl -fsS "http://127.0.0.1:$QA_PORT/api/version" > /tmp/mesflow-qa-version.json 2>/dev/null; then
    READY=1
    break
  fi
  sleep 1
done
if [[ "$READY" != "1" ]]; then
  systemctl status "$APP_NAME" --no-pager -l || true
  journalctl -u "$APP_NAME" -n 100 --no-pager -l >&2 || true
  fail "Service không trả lời endpoint version."
fi

ACTUAL_VERSION="$($INSTALL_DIR/.venv/bin/python - <<PY
import json
print(json.load(open('/tmp/mesflow-qa-version.json'))['version'])
PY
)"
rm -f /tmp/mesflow-qa-version.json
[[ "$ACTUAL_VERSION" == "$APP_VERSION" ]] || fail "Server đang trả version $ACTUAL_VERSION, không phải $APP_VERSION"

log "CLEAN DEPLOY THÀNH CÔNG"
echo "Version đang chạy: V$ACTUAL_VERSION"
echo "Service: $(systemctl is-active "$APP_NAME")"
echo "Giao diện: http://$QA_HOST:$QA_PORT"
echo "Kiểm tra version: mesflow-qa-center version"
echo "Xem log: mesflow-qa-center logs"
