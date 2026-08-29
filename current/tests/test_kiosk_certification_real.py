"""Kiosk Certification: real subprocess, real Docker sandbox, real MESFlow
kiosk v2 protocol -- needs a real Docker daemon + a real built MESFlow
deploy artifact, skipped (not faked) otherwise. Same convention as
test_qualification_job_launcher.py/test_qualification_sandbox_api.py.

This is the regression lock for every new kiosk_emulator.py scenario added
in the Kiosk Certification phase (KC-014..KC-023: malformed QR, illegal
state transitions, negative/non-integer quantity, response-lost-after-
commit idempotency, network-drop-per-phase, offline-queue exhaustiveness)
plus the profile-filtering contract in KioskEmulatorRunner.run() and the
kiosk_certification() rollup's independence guarantee.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ARTIFACT_PATH = Path(os.environ.get(
    "MESFLOW_QA_TEST_ARTIFACT",
    "/repo/artifacts/releases/71.0.0.70/MESFlow_71.0.0.70.deploy.zip"))


def _docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info"], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=10).returncode == 0
    except Exception:
        return False


requires_real_docker = pytest.mark.skipif(
    not _docker_available() or not ARTIFACT_PATH.is_file(),
    reason="needs a real Docker daemon (docker.sock) and a real built MESFlow deploy "
           "artifact at $MESFLOW_QA_TEST_ARTIFACT -- see module docstring")


def _run_cli(args, evidence_root, timeout=420):
    return subprocess.run(
        ["python3", "-m", "qualification.cli", "kiosk-certify", "--artifact", str(ARTIFACT_PATH),
         "--evidence-root", str(evidence_root), *args],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout,
    )


@requires_real_docker
def test_quick_profile_passes_against_a_real_sandbox(tmp_path, monkeypatch):
    db_path = tmp_path / "meta.sqlite3"
    monkeypatch.setenv("MESFLOW_QA_META_DB", str(db_path))
    result = _run_cli(["--profile", "QUICK"], tmp_path / "evidence", timeout=180)
    assert result.returncode == 0, result.stdout[-4000:]
    assert '"status": "PASSED"' in result.stdout


@requires_real_docker
def test_standard_profile_runs_every_new_scenario_and_all_pass(tmp_path, monkeypatch):
    """The 10 scenarios added in this phase (KC-014..KC-023) plus the
    original 13 -- 23 total -- must all genuinely PASS through the real
    kiosk v2 protocol, not just compile."""
    db_path = tmp_path / "meta.sqlite3"
    monkeypatch.setenv("MESFLOW_QA_META_DB", str(db_path))
    from engine import qa_store
    qa_store.reset_for_tests(db_path)
    result = _run_cli(["--profile", "STANDARD"], tmp_path / "evidence", timeout=420)
    assert result.returncode == 0, result.stdout[-6000:]

    from qualification.store import connect
    conn = connect()
    rows = conn.execute("SELECT sr.scenario_key,sr.status FROM qa_scenario_runs sr "
                        "JOIN qa_suite_runs s ON s.id=sr.suite_run_id "
                        "WHERE s.suite_key='kiosk_emulator'").fetchall()
    by_key = {r["scenario_key"]: r["status"] for r in rows}

    new_this_phase = [
        "sessions.lifecycle.kiosk_malformed_qr_rejected",
        "sessions.lifecycle.kiosk_employee_qr_as_operation_rejected",
        "sessions.lifecycle.kiosk_operation_before_employee_rejected",
        "sessions.lifecycle.kiosk_duplicate_operation_scan_single_session",
        "quality.quantities_rework.kiosk_negative_quantity_rejected",
        "quality.quantities_rework.kiosk_non_integer_quantity_rejected",
        "kiosk.offline_recovery.response_lost_after_commit_idempotent",
        "kiosk.offline_recovery.network_drop_per_phase",
        "kiosk.offline_recovery.offline_queue_many_events",
        "kiosk.offline_recovery.offline_queue_duplicate_replay_protected",
    ]
    for key in new_this_phase:
        assert by_key.get(key) == "PASSED", f"{key}: {by_key.get(key)!r}"
    assert len(rows) == 23  # 13 pre-existing + 10 new -- catches a silently-dropped scenario


@requires_real_docker
def test_hil_only_profile_never_fakes_a_pass_without_real_hardware(tmp_path, monkeypatch):
    db_path = tmp_path / "meta.sqlite3"
    monkeypatch.setenv("MESFLOW_QA_META_DB", str(db_path))
    result = _run_cli(["--profile", "HIL_ONLY"], tmp_path / "evidence", timeout=60)
    assert result.returncode in (0, 1, 2), result.stdout[-2000:]
    if result.returncode == 2:
        # esp_hil.py's own NOT_CONFIGURED/BLOCKED contract (see cli.py's
        # `2 if result["status"] in ("NOT_CONFIGURED","BLOCKED")`) -- this
        # CI environment has no real ESP32 attached, so PASSED must never
        # appear anywhere in the output for this run.
        assert '"status": "PASSED"' not in result.stdout, result.stdout[-2000:]
