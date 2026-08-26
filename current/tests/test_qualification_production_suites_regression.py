"""Regression safety net for the production-policy suites added 2026-08-26
(build_integrity, critical_unit, upgrade, recovery, kiosk_emulator,
test_deployment, post_deploy_smoke, scenario replay, coverage snapshots).
Docker-free: each test exercises the real logic that doesn't require a
live Docker daemon (namespace-safety guards, identity checks, isolation
regex, persistence round-trips, idempotency-queue mechanics) -- the
Docker-dependent paths (an actual deploy/restart/compose-up) are proven
separately by the real end-to-end run-artifact dry runs, not re-mocked
here as fake evidence.
"""
from __future__ import annotations

import re
import uuid
from unittest.mock import MagicMock

import pytest

from engine import qa_store
from qualification.coverage import read_snapshot, report, snapshot
from qualification.deployment import DeploymentError
from qualification.kiosk_emulator import KioskV2Client
from qualification.recovery import _require_namespace_owned
from qualification.replay import replay_scenario
from qualification.service import QualificationError, QualificationService
from qualification.store import connect, now
from qualification.test_deployment import FORBIDDEN_NAME_PATTERNS


@pytest.fixture
def service(tmp_path):
    qa_store.reset_for_tests(tmp_path / "meta.sqlite3")
    return QualificationService()


def _artifact(service, tmp_path, content=b"payload-bytes"):
    path = tmp_path / "art.zip"
    path.write_bytes(content)
    return service.register_artifact(application_version="1.0.0", git_commit="deadbeef", path=path)


def _environment(service, suffix=""):
    return service.attest_environment(name=f"env{suffix}", kind="QA", target_url="http://127.0.0.1:9",
                                      database_identity=f"db/x{suffix}", destructive_allowed=True)


def _insert_scenario(conn, run_id, *, suite_key, scenario_key, status, layer="api", required=1):
    suite_id = f"suite-{uuid.uuid4().hex}"
    conn.execute("""INSERT INTO qa_suite_runs(id,qualification_run_id,suite_key,layer,required,status,
      started_at,command_json) VALUES(?,?,?,?,?,?,?,'[]')""", (suite_id, run_id, suite_key, layer, required, status, now()))
    scenario_id = f"scenario-{uuid.uuid4().hex}"
    conn.execute("""INSERT INTO qa_scenario_runs(id,suite_run_id,scenario_key,scenario_version,driver,status,started_at)
      VALUES(?,?,?,?,?,?,?)""", (scenario_id, suite_id, scenario_key, "v1", "API", status, now()))
    conn.commit()
    return suite_id, scenario_id


# --- recovery: namespace safety ---------------------------------------------

def test_recovery_refuses_a_container_outside_its_own_namespace():
    with pytest.raises(DeploymentError, match="not part of namespace"):
        _require_namespace_owned("mesflow-qualification-abc123", "mesflow-qualification-abc123-app", "mesflow-postgres")


def test_recovery_refuses_an_unrecognized_namespace_prefix():
    with pytest.raises(DeploymentError, match="unrecognized namespace"):
        _require_namespace_owned("some-other-project", "some-other-project-app")


def test_recovery_accepts_containers_genuinely_inside_its_own_namespace():
    _require_namespace_owned("mesflow-qualification-abc123", "mesflow-qualification-abc123-app",
                             "mesflow-qualification-abc123-db")  # must not raise


# --- test_deployment: the isolation-name safety regex -----------------------

def test_deployment_isolation_regex_catches_a_real_production_container_name():
    resolved = "services:\n  mesflow:\n    container_name: mesflow-app\n    image: mesflow-app:71.0.0.206\n"
    hits = [p for p in FORBIDDEN_NAME_PATTERNS if re.search(p, resolved, re.MULTILINE)]
    assert hits, "a literal production container_name must be caught, not waved through"


def test_deployment_isolation_regex_catches_a_real_production_network_name():
    resolved = "networks:\n  default:\n    name: mesflow_network\n    external: false\n"
    hits = [p for p in FORBIDDEN_NAME_PATTERNS if re.search(p, resolved, re.MULTILINE)]
    assert hits, "the shared production network name must be caught"


def test_deployment_isolation_regex_does_not_false_positive_on_the_image_tag_or_an_isolated_alias():
    # Regression for a real false positive found by hand: the substring
    # "mesflow-app" legitimately appears in `image: mesflow-app:<version>`
    # and in an isolated network's own DNS alias -- neither is a collision
    # risk once container_name/network name are genuinely overridden.
    resolved = ("services:\n  mesflow:\n    container_name: qual-app\n"
               "    image: mesflow-app:71.0.0.206\n    networks:\n      edge:\n        aliases:\n          - mesflow-app\n"
               "networks:\n  edge:\n    name: qual-edge\n    external: false\n")
    hits = [p for p in FORBIDDEN_NAME_PATTERNS if re.search(p, resolved, re.MULTILINE)]
    assert not hits, f"benign occurrences must not trip the safety check: {hits}"


# --- coverage snapshot persistence ------------------------------------------

def test_coverage_snapshot_persists_and_is_readable_independent_of_report():
    from engine import qa_store as _qa_store
    import tempfile
    from pathlib import Path
    tmp = Path(tempfile.mkdtemp())
    _qa_store.reset_for_tests(tmp / "meta.sqlite3")
    service = QualificationService()
    artifact = _artifact(service, tmp)
    environment = _environment(service)
    run = service.start_run(artifact_id=artifact["id"], environment_id=environment["id"], profile="full",
                            dataset_version="mesflow-fixture-v1", scenario_set_version="v1")
    conn = connect()
    _insert_scenario(conn, run["id"], suite_key="api_contract", scenario_key="auth.sessions_roles.api_contract", status="PASSED")

    saved = snapshot(run["id"])
    assert saved["artifact_sha256"] == artifact["sha256"]
    assert saved["total_features"] > 0
    # One PASSED scenario alone doesn't satisfy every required_layer/driver
    # for its feature (see features.json), so it legitimately shows as
    # partially covered, not fully covered -- this asserts the snapshot
    # reflects that honestly rather than over-crediting a single scenario.
    assert saved["partially_covered_features"] >= 1

    # Independent read-back, not a recomputation -- report() is never
    # called again here, only the persisted row.
    read_back = read_snapshot(run["id"])
    assert read_back is not None
    assert read_back["covered_features"] == saved["covered_features"]
    assert read_back["snapshot"]["total_features"] == saved["total_features"]


def test_coverage_snapshot_missing_for_unknown_run_returns_none_not_an_error():
    assert read_snapshot("run-does-not-exist") is None


def test_coverage_snapshot_rejects_unknown_run_id():
    with pytest.raises(ValueError, match="unknown qualification run"):
        snapshot("run-does-not-exist")


# --- scenario replay: identity/isolation guards -----------------------------

def test_replay_scenario_blocks_when_artifact_bytes_changed_since_the_original_run(service, tmp_path):
    artifact = _artifact(service, tmp_path, content=b"original-bytes")
    environment = _environment(service)
    run = service.start_run(artifact_id=artifact["id"], environment_id=environment["id"], profile="full",
                            dataset_version="mesflow-fixture-v1", scenario_set_version="v1")
    conn = connect()
    _, scenario_run_id = _insert_scenario(conn, run["id"], suite_key="api_contract",
                                          scenario_key="auth.sessions_roles.api_contract", status="FAILED")

    # Mutate the artifact file in place -- same path, different bytes.
    artifact_path = tmp_path / "art.zip"
    artifact_path.write_bytes(b"tampered-bytes-entirely")

    with pytest.raises(DeploymentError, match="BLOCKED.*no longer match"):
        replay_scenario(scenario_run_id, tmp_path / "evidence")


def test_replay_scenario_blocks_when_artifact_file_is_gone(service, tmp_path):
    artifact = _artifact(service, tmp_path)
    environment = _environment(service)
    run = service.start_run(artifact_id=artifact["id"], environment_id=environment["id"], profile="full",
                            dataset_version="mesflow-fixture-v1", scenario_set_version="v1")
    conn = connect()
    _, scenario_run_id = _insert_scenario(conn, run["id"], suite_key="api_contract",
                                          scenario_key="auth.sessions_roles.api_contract", status="FAILED")
    (tmp_path / "art.zip").unlink()

    with pytest.raises(DeploymentError, match="BLOCKED.*no longer exists"):
        replay_scenario(scenario_run_id, tmp_path / "evidence")


def test_replay_scenario_rejects_an_unsupported_suite_key(service, tmp_path):
    artifact = _artifact(service, tmp_path)
    environment = _environment(service)
    run = service.start_run(artifact_id=artifact["id"], environment_id=environment["id"], profile="full",
                            dataset_version="mesflow-fixture-v1", scenario_set_version="v1")
    conn = connect()
    _, scenario_run_id = _insert_scenario(conn, run["id"], suite_key="build_integrity",
                                          scenario_key="build_integrity.artifact_sha256", status="FAILED")
    with pytest.raises(QualificationError, match="does not know how to replay"):
        replay_scenario(scenario_run_id, tmp_path / "evidence")


def test_replay_scenario_unknown_scenario_run_id_raises(service, tmp_path):
    with pytest.raises(QualificationError, match="unknown scenario_run_id"):
        replay_scenario("scenario-does-not-exist", tmp_path / "evidence")


# --- kiosk_emulator: offline queue / idempotency mechanics ------------------

def test_kiosk_client_queues_events_while_offline_and_replays_in_order_on_reconnect():
    http = MagicMock()
    client = KioskV2Client(http, "http://target", "device-1")
    client.go_offline()
    status1, r1 = client.scan("WF|EMP|QA-EMP-001")
    status2, r2 = client.scan("WF|OP|QA-PO-RUN-OP")
    assert status1 == status2 == "QUEUED"
    assert r1["queue_size"] == 1 and r2["queue_size"] == 2
    http.post.assert_not_called()  # nothing sent over the wire while offline

    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {"accepted": True, "state": {"name": "WAIT_OPERATION", "version": 1}, "view": {}}
    http.post.return_value = fake_response

    results = client.reconnect_and_replay()
    assert len(results) == 2
    assert http.post.call_count == 2
    assert client.queue == []  # queue drained


def test_kiosk_client_replaying_the_same_event_id_produces_identical_envelopes():
    # The real idempotency guard lives server-side (kiosk_v2._store_event's
    # payload_hash comparison) -- what this client MUST get right is
    # sending byte-identical envelopes for the same event_id, or the
    # server-side guard could never recognize them as the same request.
    http = MagicMock()
    client = KioskV2Client(http, "http://target", "device-1")
    body1 = client._envelope("SCAN", {"raw": "WF|EMP|QA-EMP-001"}, "fixed-id")
    client.seq -= 1  # _envelope() advances seq as a side effect; reset to reproduce the exact same envelope
    body2 = client._envelope("SCAN", {"raw": "WF|EMP|QA-EMP-001"}, "fixed-id")
    assert body1["event"]["event_id"] == body2["event"]["event_id"] == "fixed-id"
    assert body1["payload"] == body2["payload"]
    assert body1["device"] == body2["device"] == {"device_id": "device-1", "hardware_id": "device-1"}
