"""recovery: controlled software fault/recovery qualification
(production-policy suite). Operates ONLY on containers already belonging
to the qualification namespace passed in -- never touches anything else on
the host Docker daemon (the same safety boundary
ArtifactDeployment.destroy() already enforces, checked again here
independently since this module restarts/stops live containers, a more
dangerous operation than removing ones this process itself just created).
"""
from __future__ import annotations

import json
import subprocess
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any

import requests

from .database import DeterministicDatabase
from .deployment import DeploymentError, _run
from .evidence import EvidenceStore
from .integrity_runner import IntegrityRunner
from .scenario_runner import ScenarioRunner
from .store import connect, now

_DEVNULL = subprocess.DEVNULL


def _require_namespace_owned(namespace: str, *containers: str) -> None:
    if not namespace.startswith("mesflow-qualification-") and not namespace.startswith("mesflow-upgrade-"):
        raise DeploymentError(f"refusing to run recovery scenarios against an unrecognized namespace: {namespace}")
    for container in containers:
        if not container.startswith(namespace):
            raise DeploymentError(f"refusing to touch {container!r}: not part of namespace {namespace!r}")


class RecoveryRunner:
    def __init__(self, evidence_root: Path):
        self.conn = connect()
        self.evidence = EvidenceStore(evidence_root)
        self.evidence_root = evidence_root
        self._runner = ScenarioRunner(evidence_root, scenario_version="recovery-v1",
                                      driver="RECOVERY", evidence_kind="RECOVERY_EVIDENCE")
        self._integrity = IntegrityRunner(evidence_root)

    def _scenario(self, suite_id: str, run_id: str, key: str, fn) -> dict[str, Any]:
        return self._runner.run(suite_id, run_id, key, fn)

    @staticmethod
    def _wait_ready(app: str, target_url: str, timeout_seconds: int = 120) -> dict[str, Any]:
        deadline = time.time() + timeout_seconds
        last_error = ""
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"{target_url}/api/system/ready", timeout=3) as response:
                    ready = json.load(response)
                if ready.get("ok"):
                    return ready
            except Exception as exc:
                last_error = str(exc)
            time.sleep(1)
        raise DeploymentError(f"{app} did not recover within {timeout_seconds}s: {last_error}")

    def _invariants_ok(self, run_id: str, db_container: str) -> dict[str, Any]:
        # Delegates to the shared IntegrityRunner (spec: "Integrity Runner
        # consolidation" -- do not implement separate invariant logic per
        # scenario type). Previously this method hand-rolled its OWN,
        # SMALLER 3-check subset of live.py's 5-check invariant set (missing
        # SESSION_TEMPORAL_ORDER and NO_DUPLICATE_QUANTITY_MOVEMENT
        # entirely) and never persisted anything to qa_invariant_results --
        # a recovery scenario's invariant check was invisible outside of
        # whatever exception it raised. Both gaps are real bugs this fixes:
        # recovery now gets the exact same invariant coverage the normal
        # workflow suite proves, and every check (pass or fail) is
        # queryable evidence afterwards, same as any other suite.
        results = self._integrity.assert_ok(run_id, db_container, context="post-interruption")
        return {"checks": list(results), "violations": 0}

    def run(self, run_id: str, deployment: dict[str, Any], *, suite_key: str = "recovery") -> dict[str, Any]:
        namespace = deployment["namespace"]
        app, db = deployment["app_container"], deployment["db_container"]
        target_url = deployment["target_url"]
        _require_namespace_owned(namespace, app, db)

        suite_id = f"suite-{uuid.uuid4().hex}"
        self.conn.execute("""INSERT INTO qa_suite_runs(id,qualification_run_id,suite_key,layer,required,status,
          started_at,command_json) VALUES(?,?,?,'recovery',1,'RUNNING',?,'[]')""", (suite_id, run_id, suite_key, now()))
        self.conn.commit()
        results: list[dict[str, Any]] = []

        def scenario(key, fn):
            def measured():
                started = time.monotonic()
                actual = fn()
                if isinstance(actual, dict):
                    actual["recovery_time_seconds"] = round(time.monotonic() - started, 3)
                return actual
            result = self._scenario(suite_id, run_id, key, measured)
            results.append(result)
            return result

        def backend_restart():
            before = requests.get(f"{target_url}/api/system/ready", timeout=5).json()
            _run(["docker", "restart", "--time", "5", app])
            after = self._wait_ready(app, target_url)
            invariants = self._invariants_ok(run_id, db)
            probe = requests.get(f"{target_url}/api/employees", timeout=5)
            if probe.status_code != 401:  # unauthenticated read must still be correctly rejected post-restart, not silently broken open
                raise AssertionError(f"post-restart auth gate returned {probe.status_code}, expected 401")
            return {"before_version": before.get("version"), "after_version": after.get("version"), "invariants": invariants}
        scenario("recovery.backend_restart_returns_healthy_with_state_intact", backend_restart)

        def database_restart():
            _run(["docker", "restart", "--time", "5", db])
            deadline = time.time() + 90
            pg_up = False
            while time.time() < deadline:
                if subprocess.run(["docker", "exec", db, "pg_isready", "-U", "mesflow_qa", "-d", "mesflow_qa"],
                                  stdout=_DEVNULL, stderr=_DEVNULL).returncode == 0:
                    pg_up = True
                    break
                time.sleep(1)
            if not pg_up:
                raise AssertionError("PostgreSQL did not report ready after restart")
            after = self._wait_ready(app, target_url, timeout_seconds=120)
            invariants = self._invariants_ok(run_id, db)
            return {"postgres_recovered": True, "app_recovered_version": after.get("version"), "invariants": invariants}
        scenario("recovery.database_restart_app_recovers_and_data_intact", database_restart)

        def bounded_unavailability():
            _run(["docker", "stop", "--time", "3", app])
            distinguishable = False
            try:
                requests.get(f"{target_url}/api/system/ready", timeout=3)
            except requests.RequestException:
                distinguishable = True  # a real failure must read as a failure, not silently as success
            if not distinguishable:
                raise AssertionError("target still answered while the app container was stopped -- test setup invalid")
            _run(["docker", "start", app])
            after = self._wait_ready(app, target_url, timeout_seconds=120)
            return {"outage_correctly_detected_as_failure": distinguishable, "recovered_version": after.get("version")}
        scenario("recovery.bounded_unavailability_is_detected_and_recovers", bounded_unavailability)

        def network_partition_and_reconnect():
            # Connection-reset / gateway-failure class of fault: unlike
            # bounded_unavailability (the process is stopped), the
            # container keeps running but is cut off the network entirely
            # -- closer to what a real "kiosk network loss" or a backend's
            # own upstream gateway failure looks like from a caller's side.
            network = deployment["network"]
            _run(["docker", "network", "disconnect", "-f", network, app])
            partitioned = False
            try:
                requests.get(f"{target_url}/api/system/ready", timeout=3)
            except requests.RequestException:
                partitioned = True
            if not partitioned:
                raise AssertionError("target still answered while network-partitioned -- test setup invalid")
            _run(["docker", "network", "connect", network, app])
            after = self._wait_ready(app, target_url, timeout_seconds=90)
            invariants = self._invariants_ok(run_id, db)
            return {"partition_correctly_detected_as_failure": partitioned,
                   "reconnected_version": after.get("version"), "invariants": invariants}
        scenario("recovery.network_partition_and_reconnect", network_partition_and_reconnect)

        def app_stall_then_resume():
            # Latency/timeout class of fault: SIGSTOP-freeze the app
            # process (docker pause) so requests hang rather than fail
            # outright, then resume it -- a caller using a real bounded
            # client timeout must see that as a timeout, not silently wait
            # forever; a caller using a long timeout must see it recover
            # cleanly once resumed.
            _run(["docker", "pause", app])
            timed_out = False
            try:
                requests.get(f"{target_url}/api/system/ready", timeout=2)
            except requests.exceptions.Timeout:
                timed_out = True
            except requests.RequestException:
                timed_out = True  # a connection-level failure while paused is also an acceptable, honest signal
            _run(["docker", "unpause", app])
            if not timed_out:
                raise AssertionError("a bounded-timeout client did not observe the stall -- test setup invalid")
            after = self._wait_ready(app, target_url, timeout_seconds=60)
            return {"stall_correctly_timed_out": timed_out, "resumed_version": after.get("version")}
        scenario("recovery.app_stall_then_resume", app_stall_then_resume)

        def duplicate_request_after_recovery():
            # Reuses the exact idempotency rule mes_workflows.duplicate()
            # already proves once per run -- re-checked here specifically
            # AFTER two real interruptions, to prove the guard itself
            # survived backend/DB restarts, not just cold-start behavior.
            session = requests.Session()
            login = session.post(f"{target_url}/api/auth/login", json={"username": "admin", "password": "Admin@123456"}, timeout=10)
            if login.status_code != 200 or not login.json().get("ok"):
                raise AssertionError(f"login failed after recovery interruptions: {login.status_code} {login.text[:300]}")
            database = DeterministicDatabase(self.evidence_root)
            ids = database.query_json(db, "SELECT id FROM employees WHERE employee_no='QA-EMP-001'")
            if not ids:
                return {"skipped": "fixture employee QA-EMP-001 not present (fixture not seeded for this deployment)"}
            operation = database.query_json(db, "SELECT id FROM operations WHERE code='QA-PO-RUN-OP'")
            if not operation:
                return {"skipped": "fixture operation QA-PO-RUN-OP not present"}
            body = {"employee_id": ids[0]["id"], "operation_id": operation[0]["id"], "request_id": "QA-RECOVERY-DUP-START"}
            def post_start():
                r = session.post(f"{target_url}/api/work-sessions/start", json=body, timeout=10)
                return r.status_code, r.json()
            first_status, first = post_start()
            if first_status != 201:
                raise AssertionError(f"first start request failed ({first_status}): {first} -- QA-EMP-001 may already "
                                     f"have an open session left dangling by an earlier suite in this same run")
            second_status, second = post_start()
            if not second.get("idempotent_replay"):
                raise AssertionError("second identical request_id after recovery was NOT recognized as an idempotent replay")
            count = database.query_json(db, "SELECT count(*) n FROM work_sessions WHERE start_request_id='QA-RECOVERY-DUP-START'")[0]["n"]
            if count != 1:
                raise AssertionError(f"duplicate request created {count} sessions after recovery, expected exactly 1")
            return {"first_session_id": first.get("session", {}).get("id"), "duplicate_row_count": count}
        scenario("recovery.duplicate_request_protection_survives_interruptions", duplicate_request_after_recovery)

        status = "FAILED" if any(r["status"] == "FAILED" for r in results) else "PASSED"
        self.conn.execute("UPDATE qa_suite_runs SET status=?,finished_at=? WHERE id=?", (status, now(), suite_id))
        self.conn.commit()
        return {"suite_id": suite_id, "status": status, "scenarios": results}
