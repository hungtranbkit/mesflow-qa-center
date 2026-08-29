#!/usr/bin/env bash
# Universal CI/CD Standard V1 preflight for QA Center (see
# ../docs/CI_CD_STANDARD.md, ../AGENTS.md). Read-only checks only -- never
# installs packages, never touches Docker, never touches any live target.
#
# Minimal, per the onboarding task's own guidance: PROJECT.yaml parses,
# VERSION is present, required files exist, and the existing
# current/scripts/check-version-contract.sh sync check (reused, not
# duplicated -- it already checks current/VERSION against current/agent.py).
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

fail=0
check(){ local label="$1"; shift; if "$@" >/dev/null 2>&1; then echo "PASS  $label"; else echo "FAIL  $label"; fail=1; fi; }

echo "===== QA Center preflight ====="

check "PROJECT.yaml parses"      python3 -c "import yaml; yaml.safe_load(open('PROJECT.yaml'))"
check "VERSION present"          test -s VERSION
check "current/ source root present" test -d current
check "current/pytest.ini present"   test -f current/pytest.ini
check "current/requirements.txt present" test -f current/requirements.txt
check "current/agent.py present" test -f current/agent.py

echo
echo "-- version contract (current/VERSION vs current/agent.py APP_VERSION) --"
if (cd current && sh scripts/check-version-contract.sh); then
  echo "PASS  version contract"
else
  echo "FAIL  version contract"
  fail=1
fi

echo
if [ "$fail" -eq 0 ]; then echo "PREFLIGHT PASS"; else echo "PREFLIGHT FAIL"; fi
exit "$fail"
