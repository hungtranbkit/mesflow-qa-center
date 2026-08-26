from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from engine import qa_store
from qualification.policy import evaluate
from qualification.runner import QualificationRunner
from qualification.service import QualificationError, QualificationService


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    qa_store.reset_for_tests(tmp_path / "meta.sqlite3")
    return tmp_path


def test_artifact_digest_is_computed_and_changed_bytes_create_new_identity(isolated):
    source = isolated / "release.zip"
    source.write_bytes(b"artifact-one")
    service = QualificationService()
    first = service.register_artifact(application_version="1.0.0", git_commit="abc", path=source)
    assert first["sha256"] == hashlib.sha256(b"artifact-one").hexdigest()
    source.write_bytes(b"artifact-two")
    second = service.register_artifact(application_version="1.0.0", git_commit="abc", path=source)
    assert second["id"] != first["id"]
    assert second["sha256"] != first["sha256"]


def test_run_kind_defaults_to_release_qualification_and_is_persisted(isolated):
    artifact_file = isolated / "release.zip"
    artifact_file.write_bytes(b"run-kind-default")
    service = QualificationService()
    artifact = service.register_artifact(application_version="1.0.0", git_commit="abc", path=artifact_file)
    env = service.attest_environment(name="local-run-kind", kind="LOCAL", target_url="http://127.0.0.1:8080",
                                     database_identity="mesflow_qa")
    run = service.start_run(artifact_id=artifact["id"], environment_id=env["id"], profile="quick",
                            dataset_version="fixture-v1", scenario_set_version="scenario-v1")
    assert run["run_kind"] == "RELEASE_QUALIFICATION"
    manifest = service.run_manifest(run["id"])
    assert manifest["run_kind"] == "RELEASE_QUALIFICATION"


def test_run_kind_accepts_the_full_shared_run_manager_vocabulary(isolated):
    artifact_file = isolated / "release.zip"
    artifact_file.write_bytes(b"run-kind-vocab")
    service = QualificationService()
    artifact = service.register_artifact(application_version="1.0.0", git_commit="abc", path=artifact_file)
    for kind in ("FUNCTIONAL", "UI_E2E", "LONG_RUNNING_FACTORY_SIMULATION", "LOAD", "SOAK",
                 "OFFLINE_BURST", "CHAOS", "MIGRATION", "BACKUP_RESTORE", "ROLLBACK", "RELEASE_QUALIFICATION"):
        env = service.attest_environment(name=f"local-{kind.lower()}", kind="LOCAL",
                                         target_url="http://127.0.0.1:8080", database_identity="mesflow_qa",
                                         identity=f"LOCAL:run-kind-vocab:{kind}")
        run = service.start_run(artifact_id=artifact["id"], environment_id=env["id"], profile="quick",
                                dataset_version="fixture-v1", scenario_set_version="scenario-v1", run_kind=kind)
        assert run["run_kind"] == kind
        service.finish_run(run["id"])  # release the "already running" lock before the next kind


def test_run_kind_rejects_unknown_values(isolated):
    artifact_file = isolated / "release.zip"
    artifact_file.write_bytes(b"run-kind-invalid")
    service = QualificationService()
    artifact = service.register_artifact(application_version="1.0.0", git_commit="abc", path=artifact_file)
    env = service.attest_environment(name="local-invalid-kind", kind="LOCAL", target_url="http://127.0.0.1:8080",
                                     database_identity="mesflow_qa")
    with pytest.raises(QualificationError, match="invalid run_kind"):
        service.start_run(artifact_id=artifact["id"], environment_id=env["id"], profile="quick",
                          dataset_version="fixture-v1", scenario_set_version="scenario-v1", run_kind="NOT_A_REAL_KIND")


def test_destructive_environment_is_fail_closed(isolated):
    service = QualificationService()
    with pytest.raises(QualificationError, match="QA/TEST"):
        service.attest_environment(name="prod", kind="PRODUCTION", target_url="https://prod.example",
                                   database_identity="mesflow", destructive_allowed=True)


def test_empty_run_is_blocked_and_manifest_binds_identity(isolated):
    artifact_file = isolated / "release.zip"
    artifact_file.write_bytes(b"exact")
    service = QualificationService()
    artifact = service.register_artifact(application_version="2.0.0", git_commit="deadbeef", path=artifact_file)
    env = service.attest_environment(name="local", kind="LOCAL", target_url="http://127.0.0.1:8080",
                                     database_identity="mesflow_qa")
    run = service.start_run(artifact_id=artifact["id"], environment_id=env["id"], profile="quick",
                            dataset_version="fixture-v1", scenario_set_version="scenario-v1")
    manifest = service.finish_run(run["id"])
    assert manifest["status"] == "BLOCKED"
    assert manifest["artifact_sha256"] == artifact["sha256"]
    assert manifest["environment_identity"] == env["identity"]
    assert manifest["production_eligible"] is False


def test_failed_suite_records_log_evidence_and_blocks_run(isolated):
    artifact_file = isolated / "release.zip"
    artifact_file.write_bytes(b"exact")
    service = QualificationService()
    artifact = service.register_artifact(application_version="2", git_commit="a", path=artifact_file)
    env = service.attest_environment(name="qa", kind="QA", target_url="http://qa.invalid",
                                     database_identity="mesflow_qa", destructive_allowed=True)
    run = service.start_run(artifact_id=artifact["id"], environment_id=env["id"], profile="quick",
                            dataset_version="v1", scenario_set_version="v1")
    suite = QualificationRunner(isolated / "evidence").run_command_suite(
        run["id"], suite_key="intentional_failure", layer="unit",
        command=["python3", "-c", "print('EXPECTED FAILURE');raise SystemExit(7)"], cwd=isolated)
    assert suite["status"] == "FAILED"
    manifest = service.finish_run(run["id"])
    assert manifest["status"] == "FAILED"
    assert manifest["evidence"][0]["sha256"]


def test_policy_never_certifies_missing_suites(isolated):
    artifact_file = isolated / "release.zip"
    artifact_file.write_bytes(b"exact")
    service = QualificationService()
    artifact = service.register_artifact(application_version="2", git_commit="a", path=artifact_file)
    env = service.attest_environment(name="test", kind="TEST", target_url="http://test.invalid",
                                     database_identity="mesflow_test", destructive_allowed=True)
    decision = evaluate(artifact["id"], env["id"], [], policy={
        "key": "TEST", "version": "1", "required_suites": ["critical_unit"],
        "block_flaky_layers": ["unit"], "max_invariant_failures": 0,
        "require_hil_when_configured": True,
    })
    assert decision["production_eligible"] is False
    assert decision["missing_suites"] == ["critical_unit"]
