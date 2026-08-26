"""Regression tests for esp_hil.py and load_soak.py -- the two suites
added to close out the remaining production-policy gaps (real ESP32
hardware detection/HIL, and a bounded reuse of the existing virtual-
factory simulation engine for soak). Docker/hardware-free: these exercise
the pure decision logic (NOT_CONFIGURED vs BLOCKED vs PASS/FAIL branching)
with the real detection functions mocked at their own boundary, not the
suite runners' own DB/evidence plumbing (already covered by the real end-
to-end runs and by test_qualification_production_suites_regression.py's
patterns).
"""
from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from engine import qa_store
from qualification.esp_hil import EspHilRunner
from qualification.load_soak import LoadSoakRunner
from qualification.service import QualificationService
from qualification.store import connect


@pytest.fixture
def service(tmp_path):
    qa_store.reset_for_tests(tmp_path / "meta.sqlite3")
    return QualificationService()


def _artifact(service, tmp_path):
    path = tmp_path / "art.zip"
    path.write_bytes(b"fake-bytes")
    return service.register_artifact(application_version="1.0.0", git_commit="x", path=path)


def _run(service, tmp_path, suffix=""):
    artifact = _artifact(service, tmp_path)
    env = service.attest_environment(name=f"env{suffix}", kind="QA", target_url="http://x/",
                                     database_identity=f"db{suffix}", destructive_allowed=True)
    return service.start_run(artifact_id=artifact["id"], environment_id=env["id"], profile="full",
                             dataset_version="mesflow-fixture-v1", scenario_set_version="v1")


# --- esp_hil ------------------------------------------------------------

def test_esp_hil_reports_not_configured_when_no_device_present(service, tmp_path):
    run = _run(service, tmp_path)
    with patch("qualification.esp_hil.detect_serial_device", return_value={"present": False, "reason": "no port"}):
        result = EspHilRunner(tmp_path / "evidence").run(run["id"])
    assert result["status"] == "NOT_CONFIGURED"
    row = connect().execute("SELECT status,required FROM qa_suite_runs WHERE qualification_run_id=? AND suite_key='esp_hil'",
                            (run["id"],)).fetchone()
    assert row["status"] == "NOT_CONFIGURED"
    assert row["required"] == 0  # esp_hil is never unconditionally required -- only via policy.require_hil_when_configured


def test_esp_hil_reports_blocked_when_device_present_but_unreachable(service, tmp_path):
    run = _run(service, tmp_path)
    with patch("qualification.esp_hil.detect_serial_device", return_value={"present": True, "device": "/dev/ttyACM0", "is_espressif": True}), \
         patch("qualification.esp_hil.read_serial_identity", return_value={"ok": True, "alive": True, "uptime_ms": 12345}), \
         patch("qualification.esp_hil.check_http_reachable", return_value={"reachable": False, "url": "http://x", "error": "timed out"}):
        result = EspHilRunner(tmp_path / "evidence").run(run["id"])
    assert result["status"] == "BLOCKED"
    assert "not reachable" in result["reason"]


def test_esp_hil_blocked_when_reachable_but_no_backend_url_supplied(service, tmp_path):
    run = _run(service, tmp_path)
    with patch("qualification.esp_hil.detect_serial_device", return_value={"present": True}), \
         patch("qualification.esp_hil.read_serial_identity", return_value={"ok": True, "alive": True}), \
         patch("qualification.esp_hil.check_http_reachable", return_value={"reachable": True, "url": "http://x", "device_state": {}}):
        result = EspHilRunner(tmp_path / "evidence").run(run["id"], backend_url=None)
    assert result["status"] == "BLOCKED"
    assert "backend_url" in result["reason"]


# --- policy: hil_status must gate eligibility, not just presence --------

def test_policy_hil_required_and_blocked_denies_eligibility(service, tmp_path):
    from qualification.policy import evaluate
    artifact = _artifact(service, tmp_path)
    env = service.attest_environment(name="hil-policy-env", kind="QA", target_url="http://x/",
                                     database_identity="db-hil-policy", destructive_allowed=True)
    run = service.start_run(artifact_id=artifact["id"], environment_id=env["id"], profile="full",
                            dataset_version="v1", scenario_set_version="v1")
    conn = connect()
    conn.execute("""INSERT INTO qa_suite_runs(id,qualification_run_id,suite_key,layer,required,status,started_at,command_json)
      VALUES(?,?,'esp_hil','hil',0,'BLOCKED',?,'[]')""", (f"suite-{uuid.uuid4().hex}", run["id"], "2026-01-01T00:00:00"))
    conn.commit()
    decision = evaluate(artifact["id"], env["id"], [run["id"]],
                        policy={"key": "K", "version": "1", "required_suites": [], "block_flaky_layers": [],
                               "max_invariant_failures": 0, "require_hil_when_configured": True},
                        hil_configured=True)
    assert decision["production_eligible"] is False
    assert decision["hil_status"] == "BLOCKED"


def test_policy_hil_not_required_when_not_configured(service, tmp_path):
    from qualification.policy import evaluate
    artifact = _artifact(service, tmp_path)
    env = service.attest_environment(name="hil-policy-env2", kind="QA", target_url="http://x/",
                                     database_identity="db-hil-policy2", destructive_allowed=True)
    run = service.start_run(artifact_id=artifact["id"], environment_id=env["id"], profile="full",
                            dataset_version="v1", scenario_set_version="v1")
    decision = evaluate(artifact["id"], env["id"], [run["id"]],
                        policy={"key": "K", "version": "1", "required_suites": [], "block_flaky_layers": [],
                               "max_invariant_failures": 0, "require_hil_when_configured": True},
                        hil_configured=False)
    assert decision["hil_required_missing"] is False  # not on a certification rig -- HIL absence is fine


# --- load_soak ------------------------------------------------------------

def test_load_soak_blocked_when_engine_unavailable(service, tmp_path):
    run = _run(service, tmp_path)
    with patch("qualification.load_soak.RunManager", None):
        result = LoadSoakRunner(tmp_path / "evidence").run(run["id"], {"target_url": "http://x", "db_container": "db"})
    assert result["status"] == "BLOCKED"


def test_load_soak_fails_on_real_errors_reported_by_the_engine(service, tmp_path):
    run = _run(service, tmp_path)
    fake_mgr = MagicMock()
    fake_mgr.start.return_value = {"run_id": "sim-1", "status": "RUNNING", "metrics": {}}
    fake_mgr.status.return_value = {"run_id": "sim-1", "status": "STOPPED",
                                    "metrics": {"errors": 3, "business_events": 50}}
    fake_mgr.stop.side_effect = RuntimeError("already stopped")
    with patch("qualification.load_soak.RunManager", return_value=fake_mgr), \
         patch("qualification.load_soak.DeterministicDatabase") as fake_db_cls:
        fake_db_cls.return_value.query_json.return_value = []
        result = LoadSoakRunner(tmp_path / "evidence").run(
            run["id"], {"target_url": "http://x", "db_container": "db"}, window_seconds=0.1)
    assert result["status"] == "FAILED"
    assert result["metrics"]["errors"] == 3


def test_load_soak_fails_on_domain_invariant_violation_even_with_zero_engine_errors(service, tmp_path):
    run = _run(service, tmp_path)
    fake_mgr = MagicMock()
    fake_mgr.start.return_value = {"run_id": "sim-2", "status": "RUNNING", "metrics": {}}
    fake_mgr.status.return_value = {"run_id": "sim-2", "status": "RUNNING",
                                    "metrics": {"errors": 0, "business_events": 10}}
    fake_mgr.stop.return_value = {"run_id": "sim-2", "status": "STOPPED",
                                  "metrics": {"errors": 0, "business_events": 10}}
    with patch("qualification.load_soak.RunManager", return_value=fake_mgr), \
         patch("qualification.load_soak.DeterministicDatabase") as fake_db_cls:
        fake_db_cls.return_value.query_json.side_effect = [
            [{"id": 7, "good_qty": -1}],  # NEGATIVE_QUANTITIES: a real violation
            [],
        ]
        result = LoadSoakRunner(tmp_path / "evidence").run(
            run["id"], {"target_url": "http://x", "db_container": "db"}, window_seconds=0.1)
    assert result["status"] == "FAILED"
    assert result["invariant_violations"]["NEGATIVE_QUANTITIES"]


def test_load_soak_persists_seed_for_exact_replay(service, tmp_path):
    run = _run(service, tmp_path)
    fake_mgr = MagicMock()
    fake_mgr.start.return_value = {"run_id": "sim-3", "status": "RUNNING", "metrics": {}}
    fake_mgr.status.return_value = {"run_id": "sim-3", "status": "STOPPED",
                                    "metrics": {"errors": 0, "business_events": 5}}
    fake_mgr.stop.return_value = {"run_id": "sim-3", "status": "STOPPED",
                                  "metrics": {"errors": 0, "business_events": 5}}
    with patch("qualification.load_soak.RunManager", return_value=fake_mgr), \
         patch("qualification.load_soak.DeterministicDatabase") as fake_db_cls:
        fake_db_cls.return_value.query_json.return_value = []
        result = LoadSoakRunner(tmp_path / "evidence").run(
            run["id"], {"target_url": "http://x", "db_container": "db"}, window_seconds=0.1, seed=424242)
    assert result["status"] == "PASSED"
    assert result["seed"] == 424242
    assert "424242" in result["replay_hint"]
