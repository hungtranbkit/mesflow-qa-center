"""Sandbox UI backend (spec section 10) and live run observation (spec
section 11) REST routes -- exercised for real against a real deployed
sandbox (needs a real Docker daemon + a real built MESFlow deploy
artifact; skipped, not faked, otherwise -- same convention as the other
real-Docker test files in this suite).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

import agent
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
def isolated(tmp_path, monkeypatch):
    qa_store.reset_for_tests(tmp_path / "meta.sqlite3")
    # api.py's SandboxManager uses a fixed evidence_root ("reports/qualification"
    # relative to CWD) -- point it at this test's own tmp_path so the real
    # sandbox this test creates doesn't write into the real repo tree.
    monkeypatch.chdir(tmp_path)
    return tmp_path


@requires_real_docker
def test_sandbox_lifecycle_is_reachable_through_the_rest_api(isolated):
    manager = SandboxManager(Path("reports/qualification"))
    service = QualificationService()
    artifact = service.register_artifact(application_version="71.0.0.70", git_commit="sandbox-api-test",
                                         path=ARTIFACT_PATH)
    env = service.attest_environment(name="sandbox-api-test", kind="QA",
                                     target_url="http://qualification.invalid",
                                     database_identity="pending-isolated-postgresql", destructive_allowed=True,
                                     identity="QA:sandbox-api-test")
    run = service.start_run(artifact_id=artifact["id"], environment_id=env["id"], profile="quick",
                            dataset_version="fixture-v1", scenario_set_version="scenario-v1")
    deployment = manager.deploy(run["id"], ARTIFACT_PATH, fixture_version="mesflow-fixture-v1")
    client = agent.app.test_client()
    try:
        # list includes it
        listed = client.get("/api/qualification/sandboxes").get_json()
        assert listed["ok"] is True
        assert any(s["id"] == deployment["id"] for s in listed["sandboxes"])

        # detail includes a live health probe
        detail = client.get(f"/api/qualification/sandboxes/{deployment['id']}").get_json()
        assert detail["ok"] is True
        assert detail["sandbox"]["id"] == deployment["id"]
        assert detail["sandbox"]["health"]["healthy"] is True

        # logs
        logs = client.get(f"/api/qualification/sandboxes/{deployment['id']}/logs").get_json()
        assert logs["ok"] is True
        assert deployment["app_container"] in logs["logs"]

        # unknown sandbox -> 404, not a 500
        missing = client.get("/api/qualification/sandboxes/no-such-sandbox")
        assert missing.status_code == 404

        # stop -> start round-trip through the API
        stopped = client.post(f"/api/qualification/sandboxes/{deployment['id']}/stop")
        assert stopped.status_code == 200 and stopped.get_json()["sandbox"]["status"] == "STOPPED"
        started = client.post(f"/api/qualification/sandboxes/{deployment['id']}/start")
        assert started.status_code == 200 and started.get_json()["sandbox"]["status"] == "READY"

        # retain ("Keep for Debug")
        retained = client.post(f"/api/qualification/sandboxes/{deployment['id']}/retain")
        assert retained.status_code == 200
        assert retained.get_json()["sandbox"]["sandbox_type"] == "PERSISTENT"

        # resource-samples route answers even with zero samples yet
        samples = client.get(f"/api/qualification/runs/{run['id']}/resource-samples").get_json()
        assert samples["ok"] is True
        assert samples["samples"] == []

        # destroy through the API, and the row survives as history afterwards
        destroyed = client.post(f"/api/qualification/sandboxes/{deployment['id']}/destroy")
        assert destroyed.status_code == 200
        assert destroyed.get_json()["sandbox"]["status"] == "DESTROYED"
        still_visible = client.get(f"/api/qualification/sandboxes/{deployment['id']}").get_json()
        assert still_visible["ok"] is True
        assert still_visible["sandbox"]["status"] == "DESTROYED"
    finally:
        # best-effort: the test itself already destroys it above, but never
        # leave real containers behind if an assertion fails partway through
        sandbox = manager.get_sandbox(deployment["id"])
        if sandbox and sandbox["status"] != "DESTROYED":
            manager.destroy(deployment["namespace"])
