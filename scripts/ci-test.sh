#!/usr/bin/env bash
# Universal CI/CD Standard V1 test entrypoint for QA Center (see
# ../docs/CI_CD_STANDARD.md, ../AGENTS.md). Thin wrapper around the real,
# existing pytest suite in current/tests/ -- it does not duplicate or
# reimplement any test, it only selects and invokes.
#
# Scope of this gate, deliberately: QA Center's PROJECT.yaml already
# documents (see its own "local: NOT WIRED" notes) that this host may
# already be running a live mesflow-qa-center / mesflow-app deployment
# (memory: "Shared Docker daemon test risk" -- a spawned test process
# mutating a real container is a documented real incident, not a
# hypothetical). current/tests/ is a large (300+ test) suite; a real
# subset of it drives Docker directly (container reset/reseed, installer
# e2e, live agent HTTP calls against 127.0.0.1:8095/mesflow-app). Until
# QA Center has its own isolated CI sandbox (a known follow-up, same as
# the "local" deploy action -- see PROJECT.yaml), this CI gate runs only
# the tests that do NOT touch Docker or a live host/agent target, so it
# can never collide with whatever this host is really running.
#
# This is a real, currently-passing, currently-substantial test gate
# (180+ tests as of this writing) -- not a placeholder -- but it is
# intentionally not "the entire suite". Widening it is the natural next
# step once an isolated sandbox exists, not something to fake here.
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../current"

VENV_DIR="${QA_CI_VENV_DIR:-.venv-ci}"
if [ ! -d "$VENV_DIR" ]; then
  python3 -m venv "$VENV_DIR"
fi
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
pip install -q --disable-pip-version-check -r requirements.txt

# Keyword screen for "touches Docker or a live host/agent target" -- see
# the header comment. Recomputed every run so newly added test files are
# automatically included unless they match, not silently forgotten.
RISK_PATTERN='docker|subprocess\.run\(\[.docker|requests\.(get|post)\(.http://127|localhost:8095|127\.0\.0\.1:8095|mesflow-qa-center|mesflow-app'
SAFE_TEST_LIST="$(mktemp)"
trap 'rm -f "$SAFE_TEST_LIST"' EXIT
grep -L -E "$RISK_PATTERN" tests/test_*.py > "$SAFE_TEST_LIST"
SAFE_TEST_COUNT="$(wc -l < "$SAFE_TEST_LIST" | tr -d ' ')"

if [ "$SAFE_TEST_COUNT" -eq 0 ]; then
  echo "NO_SAFE_TESTS_FOUND: keyword screen matched every test file -- refusing to report a false PASS"
  exit 1
fi

echo "===== QA Center test ($SAFE_TEST_COUNT files, Docker/live-target tests excluded -- see script header) ====="
# shellcheck disable=SC2046
python3 -m pytest $(cat "$SAFE_TEST_LIST") -q
echo "TEST PASS"
