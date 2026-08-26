#!/usr/bin/env bash
# Bump QA Center's OWN version consistently across every declared location
# (see scripts/build-release.sh's own sync check -- it refuses to build if
# these two disagree):
#   - current/VERSION
#   - current/agent.py (APP_VERSION = "...")
#   - wrapper VERSION/install.sh/compose.yml and current/install.sh
#
# This ONLY edits source text. It never builds, tags, or pushes a Docker
# image -- run scripts/build-release.sh separately, when ready, to build.
#
# Usage:
#   scripts/bump-version.sh                # increment the last numeric segment (X.Y.Z -> X.Y.(Z+1))
#   scripts/bump-version.sh 1.21.0          # bump to an explicit version
#   scripts/bump-version.sh --to 1.21.0     # same, explicit flag form
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; cd "$ROOT"
die(){ echo "ERROR: $*" >&2; exit 1; }
SRC="$ROOT/current"

[[ -f "$SRC/VERSION" ]] || die "VERSION file not found: $SRC/VERSION"
current="$(tr -d '[:space:]' < "$SRC/VERSION")"
[[ "$current" =~ ^[0-9]+\.[0-9]+\.[0-9]+(\.[0-9]+)?$ ]] || die "VERSION_INVALID: current $SRC/VERSION is not X.Y.Z or X.Y.Z.W: '$current'"

if [[ "${1:-}" == "--to" ]]; then
  target="${2:-}"
else
  target="${1:-}"
fi
if [[ -z "$target" ]]; then
  target="$(python3 -c "
parts='$current'.split('.')
parts[-1]=str(int(parts[-1])+1)
print('.'.join(parts))
")"
fi
[[ "$target" =~ ^[0-9]+\.[0-9]+\.[0-9]+(\.[0-9]+)?$ ]] || die "VERSION_INVALID: target version is not X.Y.Z or X.Y.Z.W: '$target'"
[[ "$target" != "$current" ]] || die "VERSION_UNCHANGED: target equals current ($current)"

# Never move backward -- compare numeric segments, numerically, padding the
# shorter one with a trailing 0 so 1.20.3 vs 1.20.3.1 compares sanely.
python3 -c "
import sys
cur=[int(x) for x in '$current'.split('.')]
tgt=[int(x) for x in '$target'.split('.')]
n=max(len(cur),len(tgt))
cur+=[0]*(n-len(cur)); tgt+=[0]*(n-len(tgt))
sys.exit(0 if tgt>cur else 1)
" || die "VERSION_NOT_NEWER: target $target is not greater than current $current"

# Never bump onto a version number that's already frozen -- matches
# scripts/build-release.sh's own immutable-once guard
# (artifacts/qa-center/releases/<version>/release.json).
frozen="$ROOT/../artifacts/qa-center/releases/$target/release.json"
[[ ! -f "$frozen" ]] || die "VERSION_ALREADY_RELEASED: artifacts/qa-center/releases/$target/release.json already exists (frozen). Choose a different target version."

files=(current/VERSION current/agent.py current/install.sh VERSION install.sh compose.yml)
for file in "${files[@]}"; do [[ -f "$file" ]] || die "Expected file missing: $file"; done
backup="$(mktemp -d)"
cleanup(){ rm -rf "$backup"; }
restore(){ for file in "${files[@]}"; do mkdir -p "$backup/../noop" >/dev/null 2>&1 || true; cp -a "$backup/$file" "$file"; done; }
trap 'rc=$?; if [[ $rc -ne 0 ]]; then echo "QA VERSION BUMP FAILED — rolling back" >&2; restore; fi; cleanup; exit $rc' EXIT
for file in "${files[@]}"; do mkdir -p "$backup/$(dirname "$file")"; cp -a "$file" "$backup/$file"; done

python3 - "$target" <<'PY'
from pathlib import Path
import re, sys
version=sys.argv[1]
Path("current/VERSION").write_text(version,encoding="utf-8")
Path("VERSION").write_text(version+"\n",encoding="utf-8")
replacements={
 "current/agent.py":(r'^APP_VERSION = "[^"]+"$',f'APP_VERSION = "{version}"'),
 "current/install.sh":(r'^APP_VERSION="[^"]+"$',f'APP_VERSION="{version}"'),
 "install.sh":(r'^APP_VERSION="[^"]+"$',f'APP_VERSION="{version}"'),
 "compose.yml":(r'mesflow-qa-center:[0-9]+\.[0-9]+\.[0-9]+(?:\.[0-9]+)?',f'mesflow-qa-center:{version}'),
}
for name,(pattern,repl) in replacements.items():
 p=Path(name); text=p.read_text(encoding="utf-8")
 updated,count=re.subn(pattern,repl,text,count=1,flags=re.M)
 if count!=1: raise SystemExit(f"PATTERN_NOT_FOUND: {name}")
 p.write_text(updated,encoding="utf-8")
PY

# Verify both declarations actually landed on the new version -- fail loud
# rather than leaving a partially-bumped, inconsistent source tree.
fail=0
[[ "$(tr -d '[:space:]' < "$SRC/VERSION")" == "$target" ]] || { echo "VERIFY_FAILED: current/VERSION" >&2; fail=1; }
grep -qF "APP_VERSION = \"${target}\"" "$SRC/agent.py" || { echo "VERIFY_FAILED: current/agent.py" >&2; fail=1; }
[[ "$(tr -d '[:space:]' < VERSION)" == "$target" ]] || { echo "VERIFY_FAILED: VERSION" >&2; fail=1; }
grep -qF "APP_VERSION=\"${target}\"" install.sh || { echo "VERIFY_FAILED: install.sh" >&2; fail=1; }
grep -qF "APP_VERSION=\"${target}\"" current/install.sh || { echo "VERIFY_FAILED: current/install.sh" >&2; fail=1; }
grep -qF "mesflow-qa-center:${target}" compose.yml || { echo "VERIFY_FAILED: compose.yml" >&2; fail=1; }
[[ "$fail" -eq 0 ]] || die "One or more version declarations failed to update; inspect working tree before proceeding."

echo "VERSION BUMP PASS"
echo "Old version: $current"
echo "New version: $target"
echo "Files updated: current/VERSION, current/agent.py, current/install.sh, VERSION, install.sh, compose.yml"
echo "NOTE: no build was run. Build with scripts/build-release.sh when ready."
