"""Cleanup and retention (spec section 14): real regression coverage for
SandboxManager's EPHEMERAL/PERSISTENT lifecycle, destroy()'s completeness,
and reap_orphans() -- the "QA Center restart" cleanup path where a crash
can leave a qa_deployments row claiming a container is alive when it is
actually gone.

Needs a real Docker daemon and a real built MESFlow deploy artifact --
skipped, not faked, when either is unavailable (same convention as
test_parallel_sandbox_isolation.py).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from engine import qa_store
from qualification.deployment import SandboxManager
from qualification.service import QualificationService

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


@pytest.fixture
def isolated(tmp_path):
    qa_store.reset_for_tests(tmp_path / "meta.sqlite3")
    return tmp_path


def _start_run(suffix: str):
    service = QualificationService()
    artifact = service.register_artifact(application_version="71.0.0.70",
                                         git_commit=f"cleanup-retention-test-{suffix}", path=ARTIFACT_PATH)
    env = service.attest_environment(name=f"cleanup-retention-{suffix}", kind="QA",
                                     target_url="http://qualification.invalid",
                                     database_identity="pending-isolated-postgresql", destructive_allowed=True,
                                     identity=f"QA:cleanup-retention:{suffix}")
    run = service.start_run(artifact_id=artifact["id"], environment_id=env["id"], profile="quick",
                            dataset_version="fixture-v1", scenario_set_version="scenario-v1")
    return run["id"]


def _container_exists(name: str) -> bool:
    return subprocess.run(["docker", "inspect", name], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0


def _network_exists(name: str) -> bool:
    return subprocess.run(["docker", "network", "inspect", name], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0


def _volume_exists(name: str) -> bool:
    return subprocess.run(["docker", "volume", "inspect", name], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0


@requires_real_docker
def test_destroy_tears_down_every_real_resource_and_the_row_stays_visible_as_history(isolated):
    manager = SandboxManager(isolated / "evidence")
    run_id = _start_run("destroy")
    deployment = manager.deploy(run_id, ARTIFACT_PATH, fixture_version="mesflow-fixture-v1")
    assert _container_exists(deployment["app_container"])
    assert _container_exists(deployment["db_container"])
    assert _network_exists(deployment["network"])
    assert _volume_exists(deployment["volume"])

    manager.destroy(deployment["namespace"])

    # Every real resource is actually gone, not just marked so in the DB.
    assert not _container_exists(deployment["app_container"])
    assert not _container_exists(deployment["db_container"])
    assert not _network_exists(deployment["network"])
    assert not _volume_exists(deployment["volume"])

    # Cleanup and retention (spec section 14): "Retained resources must be
    # visible in UI/history" -- destroy() must never delete the sandbox's
    # own history row, only mark it DESTROYED.
    sandbox = manager.get_sandbox(deployment["id"])
    assert sandbox is not None
    assert sandbox["status"] == "DESTROYED"
    assert sandbox["destroyed_at"]
    assert any(s["id"] == deployment["id"] for s in manager.list_sandboxes())


@requires_real_docker
def test_retain_marks_persistent_and_the_sandbox_survives_stop_and_start(isolated):
    manager = SandboxManager(isolated / "evidence")
    run_id = _start_run("retain")
    deployment = manager.deploy(run_id, ARTIFACT_PATH, fixture_version="mesflow-fixture-v1")
    try:
        retained = manager.retain(deployment["id"])
        assert retained["sandbox_type"] == "PERSISTENT"
        assert retained["retained_at"]
        # PERSISTENT: "remain available until explicitly destroyed" -- stop()
        # then start() must bring back the exact same sandbox, not a fresh one.
        stopped = manager.stop(deployment["id"])
        assert stopped["status"] == "STOPPED"
        assert _container_exists(deployment["app_container"])  # docker stop, not rm
        started = manager.start(deployment["id"])
        assert started["status"] == "READY"
        assert started["id"] == deployment["id"]
        assert started["sandbox_type"] == "PERSISTENT"
    finally:
        manager.destroy(deployment["namespace"])


@requires_real_docker
def test_reap_orphans_marks_destroyed_when_containers_vanished_behind_its_back(isolated):
    manager = SandboxManager(isolated / "evidence")
    run_id = _start_run("orphan")
    deployment = manager.deploy(run_id, ARTIFACT_PATH, fixture_version="mesflow-fixture-v1")
    # Simulate a QA Center crash + manual/external cleanup that removed the
    # containers without ever calling destroy() -- the DB row is left
    # claiming READY while reality has already diverged.
    subprocess.run(["docker", "rm", "-f", deployment["app_container"], deployment["db_container"]],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["docker", "network", "rm", deployment["network"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["docker", "volume", "rm", deployment["volume"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert manager.get_sandbox(deployment["id"])["status"] == "READY"  # still claims alive, pre-reap

    reaped = manager.reap_orphans()

    assert any(s["id"] == deployment["id"] for s in reaped)
    sandbox = manager.get_sandbox(deployment["id"])
    assert sandbox["status"] == "DESTROYED"
    assert "orphaned" in sandbox["error"]

    # Idempotent: a second sweep must not re-touch an already-reaped/DESTROYED row.
    destroyed_at_first = sandbox["destroyed_at"]
    second_pass = manager.reap_orphans()
    assert not any(s["id"] == deployment["id"] for s in second_pass)
    assert manager.get_sandbox(deployment["id"])["destroyed_at"] == destroyed_at_first


@requires_real_docker
def test_reap_orphans_never_touches_a_sandbox_whose_container_is_still_alive(isolated):
    manager = SandboxManager(isolated / "evidence")
    run_id = _start_run("healthy")
    deployment = manager.deploy(run_id, ARTIFACT_PATH, fixture_version="mesflow-fixture-v1")
    try:
        reaped = manager.reap_orphans()
        assert not any(s["id"] == deployment["id"] for s in reaped)
        assert manager.get_sandbox(deployment["id"])["status"] == "READY"
    finally:
        manager.destroy(deployment["namespace"])
