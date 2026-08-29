"""Reconciliation of the diverged qa-center lineages (see
reconcile/qa-center-main): ported "Fast UI Coverage" from origin/main
(commits d042052/ef91e2d/d9a4452/c46123e) into
scenarios/realistic_factory_simulation.py, which this lineage's own 33
commits never touched (confirmed identical to the merge-base before this
port). Verifies the swap is behaviorally compatible with its only two
real callers: agent.py's external_script_worker (subprocess CLI) and
test_v1194_production_auth.py's auto-login text scan."""
from __future__ import annotations
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scenarios" / "realistic_factory_simulation.py"


def test_script_defines_the_state_generator_and_report_verifier():
    """The actual new capability from d042052: proves the port happened,
    not just that some file exists at this path."""
    text = SCRIPT.read_text(encoding="utf-8")
    assert "def create_session_states(" in text
    assert "def verify_reports(" in text


def test_script_still_accepts_every_flag_agent_py_passes():
    """agent.py's external_script_worker (the factory_simulation, non-soak
    path) invokes this script with a fixed argv -- a rewrite that drops or
    renames one of these flags would break that caller silently at
    runtime (argparse exits 2), never at import time."""
    text = SCRIPT.read_text(encoding="utf-8")
    assert "p.add_argument(\"--base-url\", required=True)" in text
    for flag in (
        "--fallback-base-url", "--username", "--password", "--workers",
        "--target-active-pos", "--planned-quantity", "--loop",
        "--interval-seconds", "--verify-ssl",
    ):
        assert f'p.add_argument("{flag}"' in text, f"agent.py depends on {flag}, missing from the ported script"


def test_script_still_prints_pass_fail_markers_agent_py_parses():
    """agent.py's external_script_worker scans stdout for the literal
    substrings "[PASS]" / "[FAIL]" / "TRACEBACK" to compute passed/failed
    counts for the live run view."""
    text = SCRIPT.read_text(encoding="utf-8")
    assert 'log("PASS"' in text
    assert 'log("FAIL"' in text


def test_script_contains_no_forbidden_auto_login():
    """Same guard as test_v1194_production_auth.py's
    test_no_auto_login_in_qa_runtime, re-asserted here as a standalone
    check specifically against the newly-ported file (that test already
    covers this file too; duplicated intentionally as a fast, isolated
    signal if this file is ever ported again independently)."""
    assert "/api/auth/test-auto-login" not in SCRIPT.read_text(encoding="utf-8")
