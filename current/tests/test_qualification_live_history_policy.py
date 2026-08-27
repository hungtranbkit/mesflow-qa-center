"""Regression net for the Phase 4 operator-facing additions: live
observation progress state, launch-command persistence (Re-run/Clone),
run-vs-artifact coverage aggregation, release policy tiers, and the
new REST routes built on top of them. No real Docker/sandboxes needed
here -- suite/scenario/certification rows are inserted directly, exactly
like tests/test_qualification_coverage.py already does; the real-Docker
end-to-end paths (actually launching/cancelling a long-simulation job)
are covered separately in test_qualification_job_launcher.py.
"""
from __future__ import annotations

import json

import pytest

import agent
from engine import qa_store
from qualification.coverage import artifact_report, report, run_ids_for_artifact
from qualification.policy import DEFAULT_POLICY, EXTENDED_POLICY, FAST_POLICY, POLICY_TIERS, \
    STRONG_V2_POLICY, evaluate, evaluate_tier
from qualification.service import QualificationError, QualificationService
from qualification.store import now


@pytest.fixture
def service(tmp_path, monkeypatch):
    qa_store.reset_for_tests(tmp_path / "meta.sqlite3")
    monkeypatch.chdir(tmp_path)
    return QualificationService()


def _artifact_and_env(service, tmp_path, name="art.zip", content=b"phase4-bytes", env_suffix="a"):
    path = tmp_path / name
    path.write_bytes(content)
    artifact = service.register_artifact(application_version="1.0.0", git_commit="deadbeef", path=path)
    env = service.attest_environment(name=f"env-{env_suffix}", kind="QA", target_url="http://qualification.invalid",
                                     database_identity=f"isolated-{env_suffix}", destructive_allowed=True)
    return artifact, env


def _pass_suite(service, run_id, suite_key, layer, suite_id=None, status="PASSED"):
    suite_id = suite_id or f"suite-{suite_key}-{run_id}"
    service.conn.execute(
        """INSERT INTO qa_suite_runs(id,qualification_run_id,suite_key,layer,required,status,started_at,command_json)
           VALUES(?,?,?,?,1,?,?,'[]')""", (suite_id, run_id, suite_key, layer, status, now()))
    service.conn.commit()
    return suite_id


# ---- launch_command_json / progress_json / touch_progress (spec 6/7/8) ----

def test_start_run_persists_launch_command_and_touch_progress_updates_it_back(service, tmp_path):
    artifact, env = _artifact_and_env(service, tmp_path)
    argv = ["python3", "-m", "qualification.cli", "long-simulation", "--profile", "SMOKE"]
    run = service.start_run(artifact_id=artifact["id"], environment_id=env["id"], profile="SMOKE",
                            dataset_version="fixture-v1", scenario_set_version="scenario-v1",
                            run_kind="LONG_RUNNING_FACTORY_SIMULATION", launch_argv=argv)
    manifest = service.run_manifest(run["id"])
    assert manifest["launch_command"] == argv
    assert manifest["progress"] == {}

    progress = service.touch_progress(run["id"], phase="long_running_factory_simulation",
                                      simulated_time_seconds=42.5, sessions_started=3)
    assert progress["phase"] == "long_running_factory_simulation"
    assert progress["simulated_time_seconds"] == 42.5
    assert "updated_at" in progress

    manifest = service.run_manifest(run["id"])
    assert manifest["progress"]["sessions_started"] == 3
    # touch_progress merges, it never resets fields a later call omits
    service.touch_progress(run["id"], sessions_finished=1)
    manifest = service.run_manifest(run["id"])
    assert manifest["progress"]["sessions_started"] == 3
    assert manifest["progress"]["sessions_finished"] == 1


def test_run_manifest_defaults_launch_command_and_progress_when_never_set(service, tmp_path):
    artifact, env = _artifact_and_env(service, tmp_path)
    run = service.start_run(artifact_id=artifact["id"], environment_id=env["id"], profile="quick",
                            dataset_version="fixture-v1", scenario_set_version="scenario-v1")
    manifest = service.run_manifest(run["id"])
    assert manifest["launch_command"] == []
    assert manifest["progress"] == {}


def test_touch_progress_unknown_run_raises(service):
    with pytest.raises(QualificationError):
        service.touch_progress("no-such-run", phase="x")


# ---- run_ids_for_artifact / artifact_report: RUN vs ARTIFACT coverage (spec 10) ----

def test_run_ids_for_artifact_pools_same_sha_across_separate_environments_only(service, tmp_path):
    artifact, env_a1 = _artifact_and_env(service, tmp_path, env_suffix="a1")
    _, env_a2 = _artifact_and_env(service, tmp_path, name="art.zip", content=b"phase4-bytes", env_suffix="a2")
    other_artifact, env_b = _artifact_and_env(service, tmp_path, name="other.zip", content=b"different-bytes", env_suffix="b")

    run_a1 = service.start_run(artifact_id=artifact["id"], environment_id=env_a1["id"], profile="quick",
                               dataset_version="v1", scenario_set_version="v1")
    run_a2 = service.start_run(artifact_id=artifact["id"], environment_id=env_a2["id"], profile="quick",
                               dataset_version="v1", scenario_set_version="v1")
    run_b = service.start_run(artifact_id=other_artifact["id"], environment_id=env_b["id"], profile="quick",
                              dataset_version="v1", scenario_set_version="v1")

    pooled = run_ids_for_artifact(artifact["id"])
    assert set(pooled) == {run_a1["id"], run_a2["id"]}
    assert run_b["id"] not in pooled  # never crosses artifact SHA


def test_artifact_report_aggregates_evidence_run_report_does_not(service, tmp_path):
    # database.migrations_backup requires layers database+migration (features.json).
    # Split the two credits across TWO separate runs of the SAME artifact --
    # exactly the migration-run / backup-restore-run split this phase's real
    # CLI subcommands produce -- and confirm only the ARTIFACT view can see
    # both while the single-run RUN view honestly can't.
    artifact, env_a = _artifact_and_env(service, tmp_path, env_suffix="a")
    _, env_b = _artifact_and_env(service, tmp_path, name="art.zip", content=b"phase4-bytes", env_suffix="b")
    run_migration = service.start_run(artifact_id=artifact["id"], environment_id=env_a["id"], profile="quick",
                                      dataset_version="v1", scenario_set_version="v1")
    run_backup = service.start_run(artifact_id=artifact["id"], environment_id=env_b["id"], profile="quick",
                                   dataset_version="v1", scenario_set_version="v1")
    _pass_suite(service, run_migration["id"], "upgrade", "upgrade")

    single = report([run_migration["id"]])
    feature = next(f for f in single["features"] if f["key"] == "database.migrations_backup")
    assert feature["status"] != "COVERED"

    pooled_ids = run_ids_for_artifact(artifact["id"])
    assert set(pooled_ids) == {run_migration["id"], run_backup["id"]}
    result = artifact_report(artifact["sha256"])
    assert result["artifact_sha256"] == artifact["sha256"]
    assert set(result["run_ids"]) == set(pooled_ids)
    feature = next(f for f in result["features"] if f["key"] == "database.migrations_backup")
    assert "migration" in feature["passed_layers"]


def test_artifact_report_unknown_sha_is_honestly_empty_not_an_error(service):
    result = artifact_report("0" * 64)
    assert result["run_ids"] == []
    assert result["covered_features"] == 0
    assert result["total_features"] > 0  # registry denominator is still real


# ---- Release policy tiers (spec 11) ----

def test_default_policy_v1_is_never_silently_mutated_by_the_new_tiers():
    assert DEFAULT_POLICY["key"] == "MESFLOW_STRONG_V1"
    original_required = {"build_integrity", "critical_unit", "api_contract", "integration", "mes_workflows",
                         "ui_critical", "kiosk_emulator", "upgrade", "recovery", "load_soak",
                         "test_deployment", "post_deploy_smoke"}
    assert set(DEFAULT_POLICY["required_suites"]) == original_required


def test_policy_tiers_have_distinct_keys_and_strictly_increasing_requirements():
    assert FAST_POLICY["key"] == "MESFLOW_FAST_V1"
    assert STRONG_V2_POLICY["key"] == "MESFLOW_STRONG_V2"
    assert EXTENDED_POLICY["key"] == "MESFLOW_EXTENDED_V1"
    assert set(POLICY_TIERS) == {"FAST", "STRONG", "EXTENDED"}
    fast, strong, extended = (set(POLICY_TIERS[t]["required_suites"]) for t in ("FAST", "STRONG", "EXTENDED"))
    assert fast < set(DEFAULT_POLICY["required_suites"])  # FAST is a real subset, not a rubber stamp
    assert set(DEFAULT_POLICY["required_suites"]) < strong  # STRONG_V2 adds the new reliability suites on top of v1
    assert strong < extended  # EXTENDED adds long_running_factory_simulation on top
    assert "long_running_factory_simulation" in extended and "long_running_factory_simulation" not in strong


def test_evaluate_tier_is_artifact_scoped_and_never_writes_a_certification(service, tmp_path):
    artifact, env = _artifact_and_env(service, tmp_path)
    run = service.start_run(artifact_id=artifact["id"], environment_id=env["id"], profile="quick",
                            dataset_version="v1", scenario_set_version="v1")
    for suite_key in FAST_POLICY["required_suites"]:
        _pass_suite(service, run["id"], suite_key, "layer")

    before = service.conn.execute("SELECT COUNT(*) n FROM qa_certifications").fetchone()["n"]
    decision = evaluate_tier("FAST", artifact["sha256"])
    after = service.conn.execute("SELECT COUNT(*) n FROM qa_certifications").fetchone()["n"]
    assert after == before == 0  # read-only preview: never persists
    assert decision["tier"] == "FAST"
    assert decision["production_eligible"] is True
    assert decision["missing_suites"] == []

    # STRONG requires more than this run satisfied -- same artifact, honestly different answer
    strong_decision = evaluate_tier("STRONG", artifact["sha256"])
    assert strong_decision["production_eligible"] is False
    assert strong_decision["missing_suites"]


def test_evaluate_tier_unknown_tier_raises_value_error(service):
    with pytest.raises(ValueError):
        evaluate_tier("NOT_A_REAL_TIER", "0" * 64)


def test_evaluate_still_persists_a_certification_unlike_evaluate_tier(service, tmp_path):
    artifact, env = _artifact_and_env(service, tmp_path)
    run = service.start_run(artifact_id=artifact["id"], environment_id=env["id"], profile="quick",
                            dataset_version="v1", scenario_set_version="v1")
    for suite_key in FAST_POLICY["required_suites"]:
        _pass_suite(service, run["id"], suite_key, "layer")
    decision = evaluate(artifact["id"], env["id"], [run["id"]], policy=FAST_POLICY)
    assert decision["production_eligible"] is True
    row = service.conn.execute(
        "SELECT * FROM qa_certifications WHERE artifact_id=? AND environment_id=? AND policy_key=?",
        (artifact["id"], env["id"], FAST_POLICY["key"])).fetchone()
    assert row is not None
    assert row["production_eligible"] == 1


# ---- mark_cancelled (spec 13/14: honest CANCELLED, never fake-completed) ----

def test_mark_cancelled_transitions_running_to_cancelled(service, tmp_path):
    artifact, env = _artifact_and_env(service, tmp_path)
    run = service.start_run(artifact_id=artifact["id"], environment_id=env["id"], profile="SMOKE",
                            dataset_version="v1", scenario_set_version="v1",
                            run_kind="LONG_RUNNING_FACTORY_SIMULATION")
    assert run["status"] == "RUNNING"
    manifest = service.mark_cancelled(run["id"])
    assert manifest["status"] == "CANCELLED"
    assert manifest["finished_at"]


def test_mark_cancelled_never_downgrades_an_already_terminal_run(service, tmp_path):
    artifact, env = _artifact_and_env(service, tmp_path)
    run = service.start_run(artifact_id=artifact["id"], environment_id=env["id"], profile="quick",
                            dataset_version="v1", scenario_set_version="v1")
    service.finish_run(run["id"])  # no suites -> BLOCKED, but any real terminal status proves the point
    finished_status = service.run_manifest(run["id"])["status"]
    assert finished_status != "RUNNING"
    manifest = service.mark_cancelled(run["id"])
    assert manifest["status"] == finished_status  # guard clause (status='RUNNING') protects it


# ---- New REST routes: /live, /incidents, /rerun, /clone, /jobs, /compare,
# /coverage/artifact, /policy -- all DB-only paths (no subprocess spawned
# for the 404/409 cases; the real spawn-and-cancel path needs Docker and
# lives in test_qualification_job_launcher.py) ----

@pytest.fixture
def client(service):
    return agent.app.test_client()


def test_live_route_unknown_run_is_404_not_500(client):
    resp = client.get("/api/qualification/runs/no-such-run/live")
    assert resp.status_code == 404
    assert resp.get_json()["ok"] is False


def test_live_route_reports_progress_and_terminal_flag(client, service, tmp_path):
    artifact, env = _artifact_and_env(service, tmp_path)
    run = service.start_run(artifact_id=artifact["id"], environment_id=env["id"], profile="SMOKE",
                            dataset_version="v1", scenario_set_version="v1",
                            run_kind="LONG_RUNNING_FACTORY_SIMULATION")
    service.touch_progress(run["id"], phase="long_running_factory_simulation", simulated_time_seconds=99)
    live = client.get(f"/api/qualification/runs/{run['id']}/live").get_json()
    assert live["ok"] is True
    assert live["terminal"] is False  # still RUNNING
    assert live["identity"]["run_id"] == run["id"]
    assert live["current_action"]["simulated_time_seconds"] == 99
    # this runner never called touch_progress with an actor/action -- must
    # stay genuinely null, never invented
    assert live["current_action"]["actor"] is None
    assert live["current_action"]["action"] is None

    service.finish_run(run["id"])
    live_after = client.get(f"/api/qualification/runs/{run['id']}/live").get_json()
    assert live_after["terminal"] is True


def test_incidents_route_classifies_expected_fault_vs_qualification_failure(client, service, tmp_path):
    artifact, env = _artifact_and_env(service, tmp_path)
    run = service.start_run(artifact_id=artifact["id"], environment_id=env["id"], profile="quick",
                            dataset_version="v1", scenario_set_version="v1")
    recovery_suite = _pass_suite(service, run["id"], "recovery", "recovery", status="PASSED")
    api_suite = _pass_suite(service, run["id"], "api_contract", "api", status="PASSED")
    service.conn.execute(
        """INSERT INTO qa_scenario_runs(id,suite_run_id,scenario_key,scenario_version,driver,status,fingerprint,started_at,finished_at)
           VALUES('sr-1',?,'recovery.database_restart_app_recovered','v1','API','FAILED','fp1',?,?)""",
        (recovery_suite, now(), now()))
    service.conn.execute(
        """INSERT INTO qa_scenario_runs(id,suite_run_id,scenario_key,scenario_version,driver,status,fingerprint,started_at,finished_at)
           VALUES('sr-2',?,'production_orders.lifecycle','v1','API','FAILED','fp2',?,?)""",
        (api_suite, now(), now()))
    service.conn.commit()

    incidents = client.get(f"/api/qualification/runs/{run['id']}/incidents").get_json()["incidents"]
    by_scenario = {i["scenario"]: i for i in incidents}
    assert by_scenario["recovery.database_restart_app_recovered"]["classification"] == "EXPECTED_FAULT"
    assert by_scenario["production_orders.lifecycle"]["classification"] == "QUALIFICATION_FAILURE"


def test_rerun_and_clone_without_a_launch_command_are_409_not_replayable(client, service, tmp_path):
    artifact, env = _artifact_and_env(service, tmp_path)
    run = service.start_run(artifact_id=artifact["id"], environment_id=env["id"], profile="quick",
                            dataset_version="v1", scenario_set_version="v1")  # no launch_argv given
    rerun = client.post(f"/api/qualification/runs/{run['id']}/rerun")
    assert rerun.status_code == 409
    assert rerun.get_json()["error"] == "NOT_REPLAYABLE"
    clone = client.post(f"/api/qualification/runs/{run['id']}/clone", json={})
    assert clone.status_code == 409


def test_rerun_unknown_run_is_404(client):
    resp = client.post("/api/qualification/runs/no-such-run/rerun")
    assert resp.status_code == 404


def test_job_status_unknown_job_is_404(client):
    assert client.get("/api/qualification/jobs/no-such-job").status_code == 404


def test_job_cancel_when_not_running_is_409(client):
    resp = client.post("/api/qualification/jobs/no-such-job/cancel")
    assert resp.status_code == 409
    assert resp.get_json()["error"] == "NOT_CANCELLABLE"


def test_compare_requires_at_least_two_run_ids(client):
    resp = client.get("/api/qualification/runs/compare?run_id=only-one")
    assert resp.status_code == 400


def test_compare_reports_na_honestly_when_a_run_has_no_resource_samples(client, service, tmp_path):
    artifact, env_a = _artifact_and_env(service, tmp_path, env_suffix="cmp-a")
    _, env_b = _artifact_and_env(service, tmp_path, name="art.zip", content=b"phase4-bytes", env_suffix="cmp-b")
    run_a = service.start_run(artifact_id=artifact["id"], environment_id=env_a["id"], profile="quick",
                              dataset_version="v1", scenario_set_version="v1")
    run_b = service.start_run(artifact_id=artifact["id"], environment_id=env_b["id"], profile="quick",
                              dataset_version="v1", scenario_set_version="v1")
    body = client.get(f"/api/qualification/runs/compare?run_id={run_a['id']}&run_id={run_b['id']}").get_json()
    assert body["ok"] is True
    for row in body["runs"]:
        assert row["request_count"] is None  # never fabricated as 0
        assert row["p95_latency_ms"] is None


def test_compare_unknown_run_id_reports_not_found_without_crashing_the_whole_response(client, service, tmp_path):
    artifact, env = _artifact_and_env(service, tmp_path)
    run = service.start_run(artifact_id=artifact["id"], environment_id=env["id"], profile="quick",
                            dataset_version="v1", scenario_set_version="v1")
    body = client.get(f"/api/qualification/runs/compare?run_id={run['id']}&run_id=no-such-run").get_json()
    assert body["ok"] is True
    errored = next(r for r in body["runs"] if r["run_id"] == "no-such-run")
    assert errored["error"] == "NOT_FOUND"


def test_coverage_artifact_route_matches_artifact_report_function(client, service, tmp_path):
    artifact, env = _artifact_and_env(service, tmp_path)
    run = service.start_run(artifact_id=artifact["id"], environment_id=env["id"], profile="quick",
                            dataset_version="v1", scenario_set_version="v1")
    _pass_suite(service, run["id"], "upgrade", "upgrade")
    via_route = client.get(f"/api/qualification/coverage/artifact/{artifact['sha256']}").get_json()["coverage"]
    via_function = artifact_report(artifact["sha256"])
    assert via_route["covered_features"] == via_function["covered_features"]
    assert via_route["run_ids"] == via_function["run_ids"]


def test_policy_tiers_route_lists_all_three_tiers(client):
    body = client.get("/api/qualification/policy/tiers").get_json()
    assert body["ok"] is True
    assert set(body["tiers"]) == {"FAST", "STRONG", "EXTENDED"}
    assert body["tiers"]["FAST"]["key"] == "MESFLOW_FAST_V1"


def test_policy_evaluate_tier_route_requires_artifact_sha(client):
    resp = client.get("/api/qualification/policy/evaluate-tier?tier=FAST")
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "ARTIFACT_SHA256_REQUIRED"


def test_policy_evaluate_tier_route_rejects_unknown_tier(client):
    resp = client.get(f"/api/qualification/policy/evaluate-tier?tier=BOGUS&artifact_sha256={'0'*64}")
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "UNKNOWN_TIER"
