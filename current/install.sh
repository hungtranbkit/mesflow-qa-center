#!/usr/bin/env bash
set -Eeuo pipefail
APP_VERSION="1.31.0"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -x "$HERE/../install.sh" && -f "$HERE/../PROJECT.yaml" ]]; then
  exec "$HERE/../install.sh" "$@"
fi
printf '%s\n' \
  "QA Center source installer: hãy chạy install.sh ở root của release package." \
  "Installer chỉ copy source vào ~/workspace/mesflow/qa-center; không build/deploy."
exit 2
