from engine import qa_store
from qualification.coverage import report
from qualification.service import QualificationService
from qualification.store import now


def test_registry_has_explicit_honest_denominator(tmp_path):
    qa_store.reset_for_tests(tmp_path / "meta.sqlite3")
    result = report([])
    assert result["total_features"] >= 20
    assert result["covered_features"] == 0
    assert result["critical_features"] > 0
    assert result["critical_feature_coverage_percent"] == 0
    assert any(item["key"] == "kiosk.offline_recovery" for item in result["features"])


def _run(tmp_path):
    qa_store.reset_for_tests(tmp_path / "meta.sqlite3")
    service = QualificationService()
    artifact_file = tmp_path / "release.zip"
    artifact_file.write_bytes(b"coverage-suite-level-credit")
    artifact = service.register_artifact(application_version="1.0.0", git_commit="abc", path=artifact_file)
    env = service.attest_environment(name="local-coverage", kind="LOCAL", target_url="http://127.0.0.1:8080",
                                     database_identity="mesflow_qa")
    run = service.start_run(artifact_id=artifact["id"], environment_id=env["id"], profile="quick",
                            dataset_version="fixture-v1", scenario_set_version="scenario-v1")
    return service, run["id"]


def test_a_passed_command_level_suite_credits_its_mapped_features_unit_layer_only(tmp_path):
    # spec section 9: command-level suites (no scenario_key at all) must be
    # able to contribute to feature coverage via an explicit mapping,
    # without fabricating driver-level evidence they never produced.
    service, run_id = _run(tmp_path)
    service.conn.execute(
        """INSERT INTO qa_suite_runs(id,qualification_run_id,suite_key,layer,required,status,started_at,command_json)
           VALUES('suite-critical-unit-1',?,'critical_unit','unit',1,'PASSED',?,'[]')""", (run_id, now()))
    service.conn.commit()

    result = report([run_id])
    by_key = {item["key"]: item for item in result["features"]}

    credited = by_key["auth.sessions_roles"]
    assert "unit" in credited["passed_layers"]
    assert "unit" not in credited["missing_layers"]
    assert {"suite_key": "critical_unit", "suite_run_id": "suite-critical-unit-1", "credited_layer": "unit"} \
        in credited["suite_level_evidence"]
    # Never covered outright from this alone -- api/integration/browser
    # layers and API/BROWSER driver evidence are still genuinely missing;
    # a suite-level layer credit must never fabricate driver coverage.
    assert credited["covered"] is False
    assert "api" in credited["missing_layers"] or "integration" in credited["missing_layers"]
    assert set(credited["missing_drivers"]) == {"API", "BROWSER"}

    # scheduling.dependencies_wip also requires 'unit' -- re-audited against
    # the real mesflow source (2026-08-27) and confirmed
    # tests/test_correctness_p2_unit.py (already in CRITICAL_UNIT_TEST_FILES)
    # directly unit-tests operation_wip()/priority_for_operation() from
    # mesflow.db.repositories.scheduling, a genuine match, so it is credited
    # the same way auth.sessions_roles is.
    scheduling = by_key["scheduling.dependencies_wip"]
    assert "unit" in scheduling["passed_layers"]
    assert "unit" not in scheduling["missing_layers"]
    assert {"suite_key": "critical_unit", "suite_run_id": "suite-critical-unit-1", "credited_layer": "unit"} \
        in scheduling["suite_level_evidence"]

    # employees.stations_equipment does NOT require the 'unit' layer at all
    # (features.json) -- proves the credit mechanism only ever touches
    # features that actually declare the mapped layer as required, never
    # blanket-applies to every feature.
    uncredited = by_key["employees.stations_equipment"]
    assert "unit" not in uncredited.get("required_layers", [])
    assert uncredited["suite_level_evidence"] == []


def test_suite_level_credit_only_applies_when_the_suite_actually_passed(tmp_path):
    service, run_id = _run(tmp_path)
    service.conn.execute(
        """INSERT INTO qa_suite_runs(id,qualification_run_id,suite_key,layer,required,status,started_at,command_json)
           VALUES('suite-critical-unit-2',?,'critical_unit','unit',1,'FAILED',?,'[]')""", (run_id, now()))
    service.conn.commit()

    result = report([run_id])
    by_key = {item["key"]: item for item in result["features"]}
    assert "unit" in by_key["auth.sessions_roles"]["missing_layers"]
    assert by_key["auth.sessions_roles"]["suite_level_evidence"] == []
