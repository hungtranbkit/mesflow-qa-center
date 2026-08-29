"""critical_unit: the second production-policy suite. Runs a curated,
authoritative subset of MESFlow's own pure unit tests -- not the entire
240+-file regression suite blindly (spec explicitly forbids that) -- picked
because each one protects domain logic in the critical list: session
lifecycle, time/shift calculations, authorization/business-rule error
codes, and duplicate/idempotency behavior.

How the list was chosen (2026-08-26 audit): MESFlow's own tests/conftest.py
already auto-marks files as unit/static/integration by whether they read
source text statically vs execute real logic; cross-referencing that with
every test file that `from mesflow.` imports application code directly
(not via HTTP, not via static source-grepping -- see
docs/... "Tests that import Python modules directly without exercising the
deployed runtime must not be counted as deployment qualification evidence"
from an earlier audit, which is about *integration* evidence specifically;
this suite is explicitly the *unit* layer, where direct-import IS the
right shape) gave the 8 files below. One genuine coverage gap was found
(SessionService's own command-validation + idempotent-replay-must-not-
double-publish rule had no unit test at all) and filled with
tests/test_critical_unit_session_service.py rather than left uncovered.

Quantity-calculation, PO/operation-progress, and repair/rework *business
rules* are NOT unit-tested here on purpose: MESFlow's own architecture
deliberately keeps those rules inside WorkSessionRepository (a DB-repository
class), per that module's own docstring ("Do NOT change MESFlow's existing
business rules unless there is clear evidence the existing implementation
is inconsistent") -- they are real, but only meaningfully testable against
a real Postgres, which is exactly what the mes_workflows suite (live.py)
already covers at the integration/workflow layer. Recorded honestly here
rather than papered over with a fake unit test that mocks away the only
thing worth testing.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .runner import QualificationRunner

# Relative to MESFLOW_SOURCE_ROOT (the nested mesflow/ app repo, not this
# qa-center repo and not the outer workspace wrapper repo).
CRITICAL_UNIT_TEST_FILES = [
    "tests/test_session_lifecycle_phase1.py",       # session lifecycle: login idle/absolute expiry
    "tests/test_correctness_p2_unit.py",             # time calculations: business-date/timezone interpretation
    "tests/test_shift_boundary_property.py",         # time calculations: shift-window boundary math (property-based)
    "tests/test_v66_domain_foundation.py",           # authorization/business rules: domain error hierarchy + event bus
    "tests/test_kiosk_reconciliation.py",             # duplicate/idempotency: DR reconciliation gap computation
    "tests/test_v67_exception_center_unit.py",        # duplicate/idempotency: repository dedup-race/stale-write guards
    "tests/test_offline_sync_repository_wiring.py",   # duplicate/idempotency: OfflineSyncRepository dedup-counting path
    "tests/test_critical_unit_session_service.py",    # session lifecycle + quantities: SessionService validation/idempotent-replay (gap filled 2026-08-26)
]

# The MESFlow test suite needs its own dependencies (pytest, hypothesis,
# etc.) plus MESFLOW_ENV/MESFLOW_SECRET_KEY/DATABASE_URL just to import
# mesflow.core.config at collection time -- see scripts/test/run-all.sh in
# the mesflow/ repo, whose exact env recipe is reused verbatim rather than
# re-derived. Reuses the same already-built test-runner image dev/CI
# already relies on instead of inventing a second one.
DEFAULT_TEST_IMAGE = os.environ.get("MESFLOW_TEST_IMAGE", "mesflow-test-tests:latest")


def run(qualification_run_id: str, evidence_root: Path, *, source_root: str | None = None,
        image: str = DEFAULT_TEST_IMAGE, test_files: list[str] | None = None,
        timeout_seconds: int = 600) -> dict[str, Any]:
    source_root = source_root or os.environ.get("MESFLOW_SOURCE_ROOT", "")
    if not source_root:
        raise RuntimeError("MESFLOW_SOURCE_ROOT is not set")
    # Deliberately NOT checking Path(source_root).is_dir() here: this
    # process runs Docker-outside-of-Docker (talks to the HOST daemon over
    # a mounted docker.sock), so `source_root` must be a path the HOST can
    # resolve, not necessarily one THIS container's own filesystem can see
    # -- the two commonly differ (see qualification/deployment.py's own
    # network-join comment for the same host-vs-container distinction). A
    # wrong path still fails loudly below, just via `docker run`'s own
    # bind-mount error instead of a false negative here.
    files = test_files if test_files is not None else CRITICAL_UNIT_TEST_FILES
    command = [
        "docker", "run", "--rm",
        "-e", "MESFLOW_ENV=test", "-e", "MESFLOW_SECRET_KEY=mesflow-test-secret-key",
        "-e", "DATABASE_URL=postgresql://mesflow:x@localhost:5432/mesflow_test",
        "-v", f"{source_root}:/app:ro", "-w", "/app", "--entrypoint", "python3", image,
        "-m", "pytest", "-q", "-v", *files,
    ]
    runner = QualificationRunner(evidence_root)
    return runner.run_command_suite(qualification_run_id, suite_key="critical_unit", layer="unit",
                                     command=command, cwd=Path("."), required=True,
                                     timeout_seconds=timeout_seconds)
