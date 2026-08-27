"""Web-triggered async job execution (spec section 8) and the bounded
NIGHTLY/EXTENDED/CONTINUOUS launch-then-cancel path (spec section 13/14),
exercised for real: a real subprocess, a real Docker sandbox, a real
SIGTERM. Needs a real Docker daemon + a real built MESFlow deploy artifact
-- skipped, not faked, otherwise (same convention as
test_qualification_sandbox_api.py).

This is the regression lock for two real bugs found and fixed while
building this: (1) cli.py had no SIGTERM handler, so a cancelled job's
own try/finally sandbox-teardown code never ran, leaking the container/
network; (2) api.py's job-runner thread unconditionally overwrote a
/cancel-set "CANCELLED" status with "FAILED" once the killed subprocess's
exit code came back non-zero.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

import agent
from engine import qa_store

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
    # Deliberately does NOT chdir the test process: _start_job spawns a REAL
    # `python3 -m qualification.cli ...` subprocess, and that subprocess
    # resolves the qualification package relative to ITS cwd -- inherited
    # from this process, so chdir-ing away from the repo root would break
    # module resolution in the child (a real failure mode this test hit
    # first-hand). DB isolation instead goes through MESFLOW_QA_META_DB, an
    # env var the child inherits and reads at import time, so parent and
    # child share the exact same isolated sqlite file without touching cwd.
    db_path = tmp_path / "meta.sqlite3"
    monkeypatch.setenv("MESFLOW_QA_META_DB", str(db_path))
    qa_store.reset_for_tests(db_path)
    return tmp_path


@requires_real_docker
def test_cancelling_a_long_running_job_tears_down_the_sandbox_and_ends_honestly_cancelled(isolated):
    client = agent.app.test_client()
    argv = ["python3", "-m", "qualification.cli", "long-simulation",
           "--artifact", str(ARTIFACT_PATH), "--evidence-root", str(isolated / "evidence"),
           "--profile", "NIGHTLY", "--fixture-version", "mesflow-fixture-v1"]

    from qualification.api import _start_job
    job = _start_job(argv)
    job_id = job["id"]

    # Wait past ArtifactDeployment.deploy() actually RETURNING, not just its
    # containers first appearing in `docker ps` -- `docker run` creates a
    # container well before deploy() finishes waiting on it to become
    # healthy, and cli.py only assigns its local `deployment` variable
    # (what the finally: cleanup destroys) once deploy() returns. Cancelling
    # any earlier leaves a real, un-referenced orphan -- a genuinely
    # different bug from the SIGTERM-handling one this test exists to lock
    # in, so wait for progress_json (only ever touched from inside the
    # polling loop, i.e. strictly after deploy() returned) instead of
    # racing the container list.
    deadline = time.monotonic() + 90
    run_id = None
    while time.monotonic() < deadline:
        runs = client.get("/api/qualification/runs").get_json()["runs"]
        nightly = [r for r in runs if r["profile"] == "NIGHTLY"]
        if nightly:
            run_id = nightly[0]["id"]
            live = client.get(f"/api/qualification/runs/{run_id}/live").get_json()
            if live.get("ok") and live["progress"].get("phase") == "long_running_factory_simulation":
                break
        time.sleep(2)
    else:
        pytest.fail("run never reached the post-deploy polling loop (progress_json) within 90s")
    assert client.get(f"/api/qualification/jobs/{job_id}").get_json()["job"]["status"] == "RUNNING"
    containers_before = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}"], capture_output=True, text=True, timeout=10
    ).stdout.splitlines()
    # Match THIS run's own containers specifically (not any unrelated
    # mesflow-qualification-* sandbox that might exist from something else
    # entirely) -- ArtifactDeployment.deploy() derives its namespace suffix
    # as re.sub(r"[^a-z0-9]","",run_id.lower())[-12:]; replicate that exactly
    # rather than guessing.
    import re as _re
    namespace_hint = _re.sub(r"[^a-z0-9]", "", run_id.lower())[-12:]
    this_run_containers = [c for c in containers_before if namespace_hint in c]
    assert this_run_containers, f"expected a real sandbox container for {run_id}, saw: {containers_before}"

    cancelled = client.post(f"/api/qualification/jobs/{job_id}/cancel").get_json()
    assert cancelled["ok"] is True
    assert cancelled["job"]["status"] == "CANCELLED"

    # Give the background thread time to notice proc.wait() return and run
    # its own finally: (sandbox teardown) + the SIGTERM handler's
    # mark_cancelled -- then assert the status STAYS CANCELLED (real bug #2)
    # and the sandbox is genuinely gone (real bug #1), not just marked so.
    deadline = time.monotonic() + 30
    final_job = None
    while time.monotonic() < deadline:
        final_job = client.get(f"/api/qualification/jobs/{job_id}").get_json()["job"]
        if final_job.get("finished_at"):
            break
        time.sleep(1)
    assert final_job is not None and final_job["finished_at"], "job never reached a finished state"
    assert final_job["status"] == "CANCELLED"  # never silently downgraded to FAILED
    assert final_job.get("run_id") is None  # process was killed before it could print its result JSON

    # Let the finally: block's real `docker rm`/network teardown actually
    # complete -- it runs in the (now-dead) subprocess's own cleanup after
    # SIGTERM, which takes a few real seconds against the Docker daemon.
    leaked: list[str] = []
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        containers_after = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}"], capture_output=True, text=True, timeout=10
        ).stdout
        leaked = [line for line in containers_after.splitlines() if namespace_hint in line]
        if not leaked:
            break
        time.sleep(2)
    assert leaked == [], f"cancel leaked real sandbox container(s): {leaked}"

    # The underlying qualification run row must also read CANCELLED, not be
    # stuck at RUNNING forever (spec 14: a QA Center restart must not
    # mistake a stopped run for a still-completing or completed one).
    runs = client.get("/api/qualification/runs").get_json()["runs"]
    nightly_runs = [r for r in runs if r["profile"] == "NIGHTLY"]
    assert nightly_runs, "expected the NIGHTLY run this job started to be visible in history"
    assert nightly_runs[0]["status"] == "CANCELLED"
