#!/usr/bin/env bash
set -Eeuo pipefail

APP_NAME="mesflow-qa-center"
APP_VERSION="1.29.0"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_USER="${MESFLOW_QA_WORKSPACE_USER:-${SUDO_USER:-$(id -un)}}"

log(){ printf '\n[QA CENTER SOURCE INSTALL V%s] %s\n' "$APP_VERSION" "$*"; }
fail(){ printf '\n[QA CENTER SOURCE INSTALL V%s] ERROR: %s\n' "$APP_VERSION" "$*" >&2; exit 1; }

id "$RUN_USER" >/dev/null 2>&1 || fail "Không tìm thấy user: $RUN_USER"
USER_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
[[ -n "$USER_HOME" ]] || fail "Không xác định được HOME của $RUN_USER"

WORKSPACE_ROOT="${MESFLOW_WORKSPACE_ROOT:-$USER_HOME/workspace/mesflow}"
TARGET_DIR="${MESFLOW_QA_WORKSPACE_DIR:-$WORKSPACE_ROOT/qa-center}"
RUN_GROUP="$(id -gn "$RUN_USER")"

# Safety: source installer may only target the qa-center child directory.
WORKSPACE_REAL="$(realpath -m "$WORKSPACE_ROOT")"
TARGET_REAL="$(realpath -m "$TARGET_DIR")"
EXPECTED_REAL="$(realpath -m "$WORKSPACE_ROOT/qa-center")"
[[ "$TARGET_REAL" == "$EXPECTED_REAL" ]] || fail "REFUSE unsafe target: $TARGET_DIR (expected $WORKSPACE_ROOT/qa-center)"
[[ "$(basename "$TARGET_REAL")" == "qa-center" ]] || fail "REFUSE target basename must be qa-center: $TARGET_DIR"
[[ "$TARGET_REAL" != "$WORKSPACE_REAL" ]] || fail "REFUSE target cannot equal workspace root"

[[ -f "$SOURCE_DIR/VERSION" ]] || fail "Gói thiếu VERSION"
PACKAGE_VERSION="$(tr -d '[:space:]' < "$SOURCE_DIR/VERSION")"
[[ "$PACKAGE_VERSION" == "$APP_VERSION" ]] || fail "Version gói $PACKAGE_VERSION không khớp installer $APP_VERSION"
[[ -f "$SOURCE_DIR/PROJECT.yaml" ]] || fail "Gói thiếu PROJECT.yaml"
[[ -d "$SOURCE_DIR/current" ]] || fail "Gói thiếu thư mục current/"

# Installer này chỉ đưa source vào workspace. Tuyệt đối không build/deploy/restart.

if [[ "$(realpath -m "$SOURCE_DIR")" == "$(realpath -m "$TARGET_DIR")" ]]; then
  log "Source đã nằm đúng workspace: $TARGET_DIR"
  echo "Không copy và không deploy gì thêm."
  exit 0
fi

command -v rsync >/dev/null 2>&1 || fail "Thiếu rsync. Installer copy-source không tự cài package hệ thống."

log "Cài source vào workspace chuẩn"
echo "Source : $SOURCE_DIR"
echo "Target : $TARGET_DIR"
echo "User   : $RUN_USER"

# Giữ các file local-only nếu workspace đã tồn tại.
TMP_DIR="$(mktemp -d)"
cleanup(){ rm -rf "$TMP_DIR"; }
trap cleanup EXIT
for keep in .env .env.local config.local.json; do
  if [[ -f "$TARGET_DIR/$keep" ]]; then
    cp -a "$TARGET_DIR/$keep" "$TMP_DIR/$keep"
  fi
done

mkdir -p "$TARGET_DIR"
rsync -a \
  --exclude '.git/' \
  --exclude '.env' \
  --exclude '.env.local' \
  --exclude 'config.local.json' \
  --exclude 'runtime/' \
  --exclude 'reports/' \
  --exclude 'logs/' \
  --exclude 'backups/' \
  --exclude 'evidence/' \
  --exclude 'node_modules/' \
  --exclude '.venv/' \
  --exclude '__pycache__/' \
  --exclude '.pytest_cache/' \
  "$SOURCE_DIR/" "$TARGET_DIR/"

for keep in .env .env.local config.local.json; do
  if [[ -f "$TMP_DIR/$keep" ]]; then
    cp -a "$TMP_DIR/$keep" "$TARGET_DIR/$keep"
  fi
done

# Nếu chạy bằng sudo, trả ownership source workspace cho user thật.
if [[ "$(id -u)" -eq 0 ]]; then
  chown -R "$RUN_USER:$RUN_GROUP" "$TARGET_DIR"
fi

INSTALLED_VERSION="$(tr -d '[:space:]' < "$TARGET_DIR/VERSION")"
[[ "$INSTALLED_VERSION" == "$APP_VERSION" ]] || fail "Copy xong nhưng VERSION=$INSTALLED_VERSION"
[[ -f "$TARGET_DIR/PROJECT.yaml" && -d "$TARGET_DIR/current" ]] || fail "Workspace thiếu PROJECT.yaml/current sau khi copy"

log "HOÀN TẤT - CHỈ COPY SOURCE"
echo "Workspace : $TARGET_DIR"
echo "Version   : $INSTALLED_VERSION"
echo
printf '%s\n' \
  "Installer KHÔNG thực hiện:" \
  "  - docker build" \
  "  - docker compose up/restart" \
  "  - tạo network" \
  "  - migrate database" \
  "  - deploy production/local" \
  "" \
  "Build/deploy tiếp theo do Deploy Agent / ProjectFlow thực hiện từ workspace này."
