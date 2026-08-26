"""Regression safety net for the real-artifact qualification flow (spec
section 10 of the takeover task). Each test locks in one specific way the
system must refuse to lie about qualification status: a scenario that never
ran, or that ran and failed, must never read back as coverage; a fixture
that doesn't match must block the run; a Production environment must never
be marked destructively-testable; a mismatched artifact must be rejected
before any container is touched; and a browser-suite failure must surface
as a real failed required suite, not a silently missing one.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

import pytest

from engine import qa_store
from qualification import cli as qualification_cli
from qualification.clock import VirtualClock
from qualification.coverage import report as coverage_report
from qualification.database import DeterministicDatabase, FixtureInitializationError
from qualification.deployment import ArtifactDeployment, DeploymentError
from qualification.service import QualificationError, QualificationService
from qualification.store import connect, now


@pytest.fixture
def service(tmp_path):
    qa_store.reset_for_tests(tmp_path / "meta.sqlite3")
    return QualificationService()


def _artifact(service, tmp_path, name="art.zip", content=b"payload-bytes"):
    path = tmp_path / name
    path.write_bytes(content)
    return service.register_artifact(application_version="1.0.0", git_commit="deadbeef", path=path)


def _environment(service, kind="QA", destructive=True, suffix=""):
    return service.attest_environment(name=f"env-{kind.lower()}{suffix}", kind=kind,
                                      target_url="http://127.0.0.1:9", database_identity=f"db/x{suffix}",
                                      destructive_allowed=destructive)


def test_production_environment_rejects_destructive_qualification(service):
    # A real MESFlow PRODUCTION target must never be attestable as a
    # destructive qualification sandbox -- only QA/TEST may be.
    with pytest.raises(QualificationError, match="destructive"):
        service.attest_environment(name="prod-1", kind="PRODUCTION", target_url="http://prod.example/",
                                    database_identity="prod-db/mesflow", destructive_allowed=True)


def test_production_environment_allows_non_destructive_attestation(service):
    # Merely recording that a PRODUCTION environment exists (read-only
    # attestation, e.g. for post-deploy smoke checks) must still be allowed.
    env = service.attest_environment(name="prod-2", kind="PRODUCTION", target_url="http://prod.example/",
                                      database_identity="prod-db/mesflow", destructive_allowed=False)
    assert env["kind"] == "PRODUCTION" and env["destructive_allowed"] == 0


def test_deploy_rejects_mismatched_artifact_before_touching_docker(monkeypatch, service, tmp_path):
    # Wrong artifact/runtime identity: bytes on disk must match the sha256
    # the qualification run was actually registered against, and that check
    # must happen BEFORE any `docker` subprocess call -- never assume a
    # container built from the wrong bytes is close enough.
    artifact = _artifact(service, tmp_path, content=b"original-bytes")
    environment = _environment(service)
    run = service.start_run(artifact_id=artifact["id"], environment_id=environment["id"], profile="full",
                            dataset_version="mesflow-fixture-v1", scenario_set_version="v1")

    def boom(*args, **kwargs):
        raise AssertionError("docker must not be invoked when artifact identity is already unproven")

    monkeypatch.setattr(qualification_cli.subprocess, "run", boom)
    tampered = tmp_path / "tampered.zip"
    tampered.write_bytes(b"different-bytes-entirely")
    deployer = ArtifactDeployment(tmp_path / "evidence")
    with pytest.raises(DeploymentError, match="do not match"):
        deployer.deploy(run["id"], tampered, fixture_version="mesflow-fixture-v1")


def test_fixture_seed_rejects_unknown_fixture_version(tmp_path):
    # Replay/seed using the wrong fixture must be refused outright, not
    # silently substitute whatever the database happened to already hold.
    database = DeterministicDatabase(tmp_path / "evidence")
    with pytest.raises(FixtureInitializationError, match="unsupported fixture version"):
        database.seed("run-x", "does-not-matter-container", "some-other-fixture-v2")


def test_fixture_seed_reports_mismatch_instead_of_passing_silently(monkeypatch, service, tmp_path):
    # DB reset/seed sanity check: if the freshly-seeded database doesn't
    # actually contain what the fixture is supposed to contain (e.g. the
    # reset silently failed, or ran against stale state), seed() must raise
    # with the concrete mismatch rather than let the run proceed.
    artifact = _artifact(service, tmp_path)
    environment = _environment(service)
    run = _insert_run(service, artifact, environment)
    database = DeterministicDatabase(tmp_path / "evidence")
    calls = {"n": 0}

    def fake_psql(self, container, sql):
        calls["n"] += 1
        if calls["n"] == 1:
            return ""  # the fixture SQL itself "ran" (call 1)
        # call 2 is the sanity-check SELECT -- return believable but wrong facts
        return json.dumps({"employees": 0, "production_orders": 0, "parts": 0, "operations": 0,
                            "active_sessions": 0, "closed_sessions": 0, "database": "mesflow_qa"})

    monkeypatch.setattr(DeterministicDatabase, "_psql", fake_psql)
    with pytest.raises(FixtureInitializationError, match="fixture sanity mismatch"):
        database.seed(run["id"], "container-x", "mesflow-fixture-v1")


def test_fixture_seed_validates_late_and_waiting_po_against_an_injected_clock(monkeypatch, service, tmp_path):
    # QA clock abstraction (spec section 5): the fixture encodes "late" /
    # "waiting" PO states as CURRENT_DATE-relative dates; seed() must
    # independently re-derive whether that's still true against an
    # explicit "now" rather than trusting the fixture's own labels. A
    # VirtualClock set to a point in time where QA-PO-LATE's due date is
    # actually still in the FUTURE must be caught, not silently accepted.
    from datetime import datetime, timedelta, timezone
    artifact = _artifact(service, tmp_path)
    environment = _environment(service)
    run = _insert_run(service, artifact, environment)
    database = DeterministicDatabase(tmp_path / "evidence")
    calls = {"n": 0}
    today = datetime.now(timezone.utc).date()
    late_due = (today - timedelta(days=2)).isoformat()
    wait_due = (today + timedelta(days=7)).isoformat()

    def fake_psql(self, container, sql):
        calls["n"] += 1
        if calls["n"] == 1:
            return ""
        return json.dumps({"employees": 3, "production_orders": 4, "parts": 4, "operations": 4,
                           "active_sessions": 1, "closed_sessions": 1, "database": "mesflow_qa",
                           "po_late_due_date": late_due, "po_wait_due_date": wait_due,
                           "po_late_status": "IN_PROGRESS", "po_wait_status": "PLANNED"})

    monkeypatch.setattr(DeterministicDatabase, "_psql", fake_psql)
    # A clock set BEFORE the fixture's "late" PO's due date -- from this
    # clock's point of view that PO is not actually late yet.
    early_clock = VirtualClock(datetime.now(timezone.utc) - timedelta(days=10))
    with pytest.raises(FixtureInitializationError, match="does not match the clock"):
        database.seed(run["id"], "container-x", "mesflow-fixture-v1", clock=early_clock)

    # The real, current clock must accept the same fixture facts (its
    # dates are genuinely relative to today).
    calls["n"] = 0
    result = database.seed(run["id"], "container-x", "mesflow-fixture-v1")
    assert result["clock_checks"]["po_late"]["ok"] is True
    assert result["clock_checks"]["po_wait"]["ok"] is True


def _insert_run(service, artifact, environment, *, profile="full"):
    return service.start_run(artifact_id=artifact["id"], environment_id=environment["id"], profile=profile,
                             dataset_version="mesflow-fixture-v1", scenario_set_version="v1")


def _insert_scenario(conn, run_id, *, scenario_key, status, driver="API", layer="api"):
    suite_id = f"suite-{uuid.uuid4().hex}"
    conn.execute("""INSERT INTO qa_suite_runs(id,qualification_run_id,suite_key,layer,required,status,
      started_at,command_json) VALUES(?,?,?,?,1,?,?,'[]')""", (suite_id, run_id, scenario_key, layer, status, now()))
    scenario_id = f"scenario-{uuid.uuid4().hex}"
    conn.execute("""INSERT INTO qa_scenario_runs(id,suite_run_id,scenario_key,scenario_version,driver,status,started_at)
      VALUES(?,?,?,?,?,?,?)""", (scenario_id, suite_id, scenario_key, "v1", driver, status, now()))
    conn.commit()
    return suite_id, scenario_id


def test_failed_scenario_never_counts_as_coverage(service, tmp_path):
    # A feature must not read back as COVERED (or credited toward any passed
    # layer/driver) just because a scenario exists for it -- only a scenario
    # that actually PASSED may count.
    artifact = _artifact(service, tmp_path)
    environment = _environment(service)
    run = _insert_run(service, artifact, environment)
    conn = connect()
    # auth.sessions_roles requires layers unit/api/integration and drivers API/BROWSER.
    _insert_scenario(conn, run["id"], scenario_key="auth.sessions_roles.api_contract", status="FAILED")
    result = coverage_report([run["id"]])
    feature = next(f for f in result["features"] if f["key"] == "auth.sessions_roles")
    assert feature["status"] in {"BLOCKED", "UNCOVERED"}
    assert not feature["covered"]
    assert feature["blocking_results"], "the failed scenario must be surfaced as a blocking result"


def test_coverage_ignores_scenarios_from_a_different_run_stale_coverage(service, tmp_path):
    # Coverage for run A must not be inflated by a PASSED scenario that
    # actually belongs to an unrelated run B -- coverage must be scoped
    # strictly to the run_ids the caller asked about.
    artifact = _artifact(service, tmp_path)
    run_a = _insert_run(service, artifact, _environment(service, suffix="-a"))
    run_b = _insert_run(service, artifact, _environment(service, suffix="-b"))
    conn = connect()
    _insert_scenario(conn, run_b["id"], scenario_key="auth.sessions_roles.api_contract", status="PASSED")
    result = coverage_report([run_a["id"]])
    feature = next(f for f in result["features"] if f["key"] == "auth.sessions_roles")
    assert not feature["supporting_scenarios"], "run B's scenario must not leak into run A's coverage"
    assert not feature["covered"]


def test_browser_suite_failure_records_a_failed_required_suite_not_silence(service, tmp_path, monkeypatch):
    # If the Playwright critical-UI suite cannot even start (missing
    # browsers, import error, etc.), the run-artifact flow must record this
    # as a real FAILED required suite -- never omit it, and never treat the
    # absence of output as an implicit pass.
    artifact = _artifact(service, tmp_path)
    environment = _environment(service)
    run = _insert_run(service, artifact, environment)

    def broken_import(*args, **kwargs):
        raise RuntimeError("Executable doesn't exist: no browsers installed")

    monkeypatch.setattr("qualification.browser.run_browser_suite", broken_import)
    result = qualification_cli._run_browser_suite_or_record_failure(run["id"], "http://127.0.0.1:1", tmp_path / "evidence")
    assert result["status"] == "FAILED"
    row = connect().execute(
        "SELECT status,required,suite_key FROM qa_suite_runs WHERE qualification_run_id=? AND suite_key='ui_critical'",
        (run["id"],)).fetchone()
    assert row is not None, "a FAILED ui_critical suite row must exist, not be silently missing"
    assert row["status"] == "FAILED" and row["required"] == 1
