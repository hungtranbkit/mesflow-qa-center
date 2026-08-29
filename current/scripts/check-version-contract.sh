#!/usr/bin/env sh
set -eu
version="$(tr -d '\r\n' < VERSION)"
python3 - "$version" <<'PY'
import re, sys
from pathlib import Path
version=sys.argv[1]
source=Path('agent.py').read_text(encoding='utf-8')
match=re.search(r'^APP_VERSION = "([^"]+)"',source,re.M)
if not match or match.group(1) != version:
    raise SystemExit(f'VERSION_MISMATCH file={version!r} app={match.group(1) if match else None!r}')
print(f'VERSION_OK {version}')
PY
