"""Scheduler reliability (spec section 5): MESFlow has no in-process
scheduler -- both real scheduled jobs (exception_reconciliation,
shift_session_reconciliation, see mesflow.cli / scripts/install-reconcile-
cron.sh) are host cron entries that `docker compose exec -T <app> python -m
mesflow.cli reconcile-<job>`. This suite runs the EXACT same commands
inside the qualification sandbox's own app container -- not a QA-only
simulation of a scheduler that doesn't exist -- and exercises the real
scheduled_job_health tracking (mesflow.core.scheduled_job) a real cron
entry would produce.

Feeds exceptions.reconciliation and health.jobs_notifications' 'recovery'
layer requirement specifically (their api/integration/workflow layers are
covered elsewhere, in live.py) -- restart-during-execution and duplicate-
worker-collision are reliability/recovery concerns, not plain API contract
checks.
"""
from __future__ import annotations

import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import requests

from .database import DeterministicDatabase
from .deployment import _run
from .evidence import EvidenceStore
from .integrity_runner import IntegrityRunner
from .scenario_runner import ScenarioRunner
from .store import connect, now

JOBS = ("exception_reconciliation", "shift_session_reconciliation")
_COMMAND_FOR_JOB = {"exception_reconciliation": "reconcile-exceptions", "shift_session_reconciliation": "reconcile-shift-sessions"}


class SchedulerReliabilityRunner:
    def __init__(self, evidence_root: Path):
        self.conn = connect()
        self.evidence = EvidenceStore(evidence_root)
        self.evidence_root = evidence_root
        self._integrity = IntegrityRunner(evidence_root)
        self._runner = ScenarioRunner(evidence_root, scenario_version="scheduler-reliability-v1",
                                      driver="API", evidence_kind="SCHEDULER_RELIABILITY_EVIDENCE")

    def _exec_job(self, app_container: str, job: str, *, timeout: int = 60) -> subprocess.CompletedProcess:
        return subprocess.run(["docker", "exec", app_container, "python", "-m", "mesflow.cli", _COMMAND_FOR_JOB[job]],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)

    def _job_health(self, db_container: str, job: str) -> dict[str, Any]:
        database = DeterministicDatabase(self.evidence_root)
        rows = database.query_json(db_container,
            f"SELECT job_name,last_status,last_started_at,last_finished_at,consecutive_failures FROM scheduled_job_health WHERE job_name='{job}'")
        if not rows:
            raise AssertionError(f"scheduled_job_health has no row for {job!r}")
        return rows[0]

    def run(self, run_id: str, deployment: dict[str, Any], *, target_job: str = "exception_reconciliation") -> dict[str, Any]:
        app_container = deployment["app_container"]
        db_container = deployment["db_container"]
        target_url = deployment["target_url"].rstrip("/")
        suite_id = f"suite-{uuid.uuid4().hex}"
        self.conn.execute("""INSERT INTO qa_suite_runs(id,qualification_run_id,suite_key,layer,required,status,
          started_at,command_json) VALUES(?,?,'scheduler_reliability','recovery',1,'RUNNING',?,'[]')""", (suite_id, run_id, now()))
        self.conn.commit()
        results: list[dict[str, Any]] = []

        def scenario(key, fn):
            result = self._runner.run(suite_id, run_id, key, fn)
            results.append(result)
            return result

        def job_runs_successfully():
            proc = self._exec_job(app_container, target_job)
            if proc.returncode != 0:
                raise AssertionError(f"reconcile-{target_job} exited {proc.returncode}: {proc.stdout.decode('utf-8', 'replace')[-1500:]}")
            health = self._job_health(db_container, target_job)
            if health["last_status"] != "SUCCESS":
                raise AssertionError(f"scheduled_job_health for {target_job} is {health['last_status']!r} after a clean run, expected SUCCESS")
            return {"job": target_job, "exit_code": proc.returncode, "health": health}
        scenario("exceptions.reconciliation.job_runs_successfully", job_runs_successfully)

        def duplicate_worker_collision():
            # Two workers picking up the SAME job at once -- the real
            # failure mode a cron misconfiguration (or a slow first run
            # overlapping the next tick) would produce. Neither invocation
            # may crash, and the job must end up in a consistent SUCCESS
            # state, not corrupted or doubly-processed.
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(self._exec_job, app_container, target_job) for _ in range(2)]
                procs = [f.result() for f in futures]
            failures = [p.returncode for p in procs if p.returncode != 0]
            if failures:
                raise AssertionError(f"a concurrent duplicate invocation of {target_job} failed: exit codes {[p.returncode for p in procs]}")
            health = self._job_health(db_container, target_job)
            if health["last_status"] != "SUCCESS":
                raise AssertionError(f"scheduled_job_health for {target_job} is {health['last_status']!r} after concurrent duplicate runs")
            return {"job": target_job, "concurrent_runs": 2, "both_exit_zero": True, "health_after": health}
        scenario("exceptions.reconciliation.duplicate_worker_collision", duplicate_worker_collision)

        def restart_during_execution():
            # A real attempt at racing a container restart against the job
            # actually running (not merely simulated): the job is started
            # in the background and the app container is restarted with no
            # deliberate delay in between. Because the real job is fast
            # against the small QA fixture, the exact "caught it mid-flight"
            # race is not guaranteed on every run -- what IS guaranteed and
            # asserted is the actual reliability property that matters: the
            # job is never left permanently stuck, and a normal run
            # afterward always recovers it to SUCCESS.
            proc = subprocess.Popen(["docker", "exec", app_container, "python", "-m", "mesflow.cli", _COMMAND_FOR_JOB[target_job]],
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            _run(["docker", "restart", "--time", "3", app_container])
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
            deadline = time.time() + 90
            ready = False
            while time.time() < deadline:
                try:
                    if requests.get(f"{target_url}/api/system/ready", timeout=3).json().get("ok"):
                        ready = True
                        break
                except requests.RequestException:
                    pass
                time.sleep(1)
            if not ready:
                raise AssertionError(f"app container did not become ready again after restart during {target_job}")
            recovery = self._exec_job(app_container, target_job)
            if recovery.returncode != 0:
                raise AssertionError(f"post-restart recovery run of {target_job} failed: exit {recovery.returncode}")
            health = self._job_health(db_container, target_job)
            if health["last_status"] != "SUCCESS":
                raise AssertionError(f"{target_job} did not recover to SUCCESS after a restart-during-execution attempt: {health}")
            return {"job": target_job, "app_restarted": True, "recovered_to_success": True, "health_after_recovery": health}
        scenario("health.jobs_notifications.job_health_recovery_after_restart", restart_during_execution)

        def integrity_after_reconciliation():
            integrity = self._integrity.check(run_id, db_container, evidence_name="scheduler-reliability-invariants")
            broken = {k: v["violations"] for k, v in integrity.items() if v["status"] == "FAILED"}
            if broken:
                raise AssertionError(f"domain invariants failed after scheduler reliability scenarios: {broken}")
            return {"checks": list(integrity)}
        scenario("health.jobs_notifications.integrity_after_reconciliation", integrity_after_reconciliation)

        status = "FAILED" if any(r["status"] == "FAILED" for r in results) else "PASSED"
        self.conn.execute("UPDATE qa_suite_runs SET status=?,finished_at=? WHERE id=?", (status, now(), suite_id))
        self.conn.commit()
        return {"suite_id": suite_id, "status": status, "scenarios": results}
