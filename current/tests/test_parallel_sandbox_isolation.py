"""Parallel isolation (spec section 13, MANDATORY): real regression
coverage proving simultaneous sandboxes never share database, volume,
network, container name, port, evidence directory, or run state --
required for simultaneous Claude/Codex/ProjectFlow/manual QA execution.

This drives SandboxManager.deploy() for real, twice, concurrently-alive
at once -- exactly like every other real-Docker verification in this
whole engagement -- against a real, already-built MESFlow deploy
artifact. Skipped (never faked) when there is no real Docker daemon
reachable or no real artifact available, e.g. a plain CI runner with no
docker.sock mounted.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import pytest

from engine import qa_store
from qualification.database import DeterministicDatabase
from qualification.deployment import SandboxManager
from qualification.evidence import EvidenceStore
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
                                         git_commit=f"parallel-isolation-test-{suffix}", path=ARTIFACT_PATH)
    env = service.attest_environment(name=f"parallel-isolation-{suffix}", kind="QA",
                                     target_url="http://qualification.invalid",
                                     database_identity="pending-isolated-postgresql", destructive_allowed=True,
                                     identity=f"QA:parallel-isolation:{suffix}")
    run = service.start_run(artifact_id=artifact["id"], environment_id=env["id"], profile="quick",
                            dataset_version="fixture-v1", scenario_set_version="scenario-v1")
    return run["id"]


@requires_real_docker
def test_two_simultaneous_sandboxes_never_share_any_resource_and_writes_do_not_cross_contaminate(isolated):
    evidence_root = isolated / "evidence"
    manager = SandboxManager(evidence_root)
    run_id_a = _start_run("a")
    run_id_b = _start_run("b")
    deployment_a = deployment_b = None
    try:
        # Both sandboxes alive at the same time -- deploy B before doing
        # anything final with A, so they genuinely coexist rather than one
        # being torn down before the other starts.
        deployment_a = manager.deploy(run_id_a, ARTIFACT_PATH, fixture_version="mesflow-fixture-v1")
        deployment_b = manager.deploy(run_id_b, ARTIFACT_PATH, fixture_version="mesflow-fixture-v1")

        # --- No shared identity of any kind ---
        assert deployment_a["id"] != deployment_b["id"]
        assert deployment_a["namespace"] != deployment_b["namespace"]
        assert deployment_a["app_container"] != deployment_b["app_container"]
        assert deployment_a["db_container"] != deployment_b["db_container"]
        assert deployment_a["network"] != deployment_b["network"]
        assert deployment_a["target_url"] != deployment_b["target_url"]
        assert not deployment_a["app_container"].startswith(deployment_b["namespace"])
        assert not deployment_b["app_container"].startswith(deployment_a["namespace"])

        # target_url itself is container-name:8080 (both sandboxes' apps
        # listen on the same fixed internal port -- that's fine, they're
        # different container names on different private networks). The
        # actual per-sandbox HOST port isolation is published_url/app_port
        # (docker run -p 127.0.0.1::8080 picks a fresh ephemeral host port
        # per container) -- that is what a future host-browser "Open
        # MESFlow" path would need to be distinct, and it genuinely is.
        port_a = urlparse(deployment_a["runtime"]["published_url"]).port
        port_b = urlparse(deployment_b["runtime"]["published_url"]).port
        assert port_a is not None and port_b is not None and port_a != port_b
        sandbox_a_port = manager.get_sandbox(deployment_a["id"])["app_port"]
        sandbox_b_port = manager.get_sandbox(deployment_b["id"])["app_port"]
        assert sandbox_a_port == port_a and sandbox_b_port == port_b

        # --- Independent run state: each sandbox row points back at its own run ---
        sandbox_a = manager.get_sandbox(deployment_a["id"])
        sandbox_b = manager.get_sandbox(deployment_b["id"])
        assert sandbox_a["qualification_run_id"] == run_id_a
        assert sandbox_b["qualification_run_id"] == run_id_b
        assert sandbox_a["qualification_run_id"] != sandbox_b["qualification_run_id"]

        # --- Independent database/volume: a write to A must never appear in B ---
        db = DeterministicDatabase(evidence_root)
        db.seed(run_id_a, deployment_a["db_container"], "mesflow-fixture-v1")
        db.seed(run_id_b, deployment_b["db_container"], "mesflow-fixture-v1")
        marker_a, marker_b = f"SANDBOX-MARKER-{run_id_a}", f"SANDBOX-MARKER-{run_id_b}"
        # query_json wraps its SQL as `SELECT ... FROM (sql) q`, which only
        # accepts a SELECT -- an UPDATE needs the raw psql path directly.
        db._psql(deployment_a["db_container"],
            f"UPDATE employees SET name='{marker_a}' WHERE id=(SELECT id FROM employees WHERE employee_no LIKE 'QA-%' ORDER BY id LIMIT 1);")
        db._psql(deployment_b["db_container"],
            f"UPDATE employees SET name='{marker_b}' WHERE id=(SELECT id FROM employees WHERE employee_no LIKE 'QA-%' ORDER BY id LIMIT 1);")

        assert db.query_json(deployment_a["db_container"], f"SELECT count(*) n FROM employees WHERE name='{marker_a}'")[0]["n"] == 1
        assert db.query_json(deployment_b["db_container"], f"SELECT count(*) n FROM employees WHERE name='{marker_b}'")[0]["n"] == 1
        # The defining proof of real storage isolation (not just distinct
        # connection strings to the same server): A's marker must be
        # totally absent from B's database, and vice versa.
        assert db.query_json(deployment_b["db_container"], f"SELECT count(*) n FROM employees WHERE name='{marker_a}'")[0]["n"] == 0
        assert db.query_json(deployment_a["db_container"], f"SELECT count(*) n FROM employees WHERE name='{marker_b}'")[0]["n"] == 0

        # --- Independent evidence directories ---
        evidence = EvidenceStore(evidence_root)
        evidence.write_json(run_id_a, "isolation-marker.json", {"owner": "a"}, kind="ISOLATION_TEST_EVIDENCE")
        evidence.write_json(run_id_b, "isolation-marker.json", {"owner": "b"}, kind="ISOLATION_TEST_EVIDENCE")
        dir_a_files = {p.name for p in (evidence_root / run_id_a).iterdir()}
        dir_b_files = {p.name for p in (evidence_root / run_id_b).iterdir()}
        assert dir_a_files, "sandbox A's evidence directory must contain its own evidence"
        assert dir_b_files, "sandbox B's evidence directory must contain its own evidence"
        assert dir_a_files.isdisjoint(dir_b_files)
    finally:
        if deployment_a:
            manager.destroy(deployment_a["namespace"])
        if deployment_b:
            manager.destroy(deployment_b["namespace"])
