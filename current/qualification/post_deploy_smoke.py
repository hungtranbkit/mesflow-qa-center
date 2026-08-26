"""post_deploy_smoke: a short, high-confidence smoke suite run right after
a deployment completes (production-policy suite). Deliberately much
smaller than the full qualification battery -- meant to answer one
question fast ("did the thing that was just deployed actually come up
correctly, end to end") rather than re-prove full business coverage.

Read-only except for exactly one disposable, fixture-scoped work session
(start+finish, uses the deterministic QA fixture's own employee/operation,
never touches real production data) -- matches "create/start/complete one
disposable workflow if safe" from the spec.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

import requests

from .evidence import EvidenceStore
from .store import connect, now


class PostDeploySmokeRunner:
    def __init__(self, evidence_root: Path):
        self.conn = connect()
        self.evidence = EvidenceStore(evidence_root)
        self.http = requests.Session()

    def _scenario(self, suite_id: str, run_id: str, key: str, fn) -> dict[str, Any]:
        scenario_id = f"scenario-{uuid.uuid4().hex}"
        self.conn.execute("""INSERT INTO qa_scenario_runs(id,suite_run_id,scenario_key,scenario_version,driver,
          status,started_at) VALUES(?,?,?,?,?,'RUNNING',?)""",
          (scenario_id, suite_id, key, "post-deploy-smoke-v1", "API", now()))
        self.conn.commit()
        status, actual, first_failure = "PASSED", {}, ""
        started = time.time()
        try:
            actual = fn() or {}
        except Exception as exc:  # noqa: BLE001
            status, first_failure = "FAILED", "assertion"
            actual = {"error_class": type(exc).__name__, "message": str(exc)}
        actual["duration_seconds"] = round(time.time() - started, 3)
        evidence = self.evidence.write_json(run_id, f"{key.replace('.', '-')}.json",
            {"scenario": key, "status": status, "actual": actual}, kind="SMOKE_EVIDENCE",
            suite_run_id=suite_id, scenario_run_id=scenario_id)
        self.conn.execute("UPDATE qa_scenario_runs SET status=?,finished_at=?,first_failing_step=?,actual_json=? WHERE id=?",
                          (status, now(), first_failure, json.dumps(actual, default=str), scenario_id))
        self.conn.execute("INSERT INTO qa_attempts(scenario_run_id,attempt_no,status,fingerprint,started_at,finished_at) "
                          "VALUES(?,1,?,?,?,?)", (scenario_id, status, "", now(), now()))
        self.conn.commit()
        return {"key": key, "status": status, "actual": actual, "evidence": evidence}

    def run(self, run_id: str, target_url: str, *, admin_password: str = "Admin@123456",
            allow_disposable_workflow: bool = True, suite_key: str = "post_deploy_smoke") -> dict[str, Any]:
        # suite_key is overridable for the same reason live.py's workflows()
        # is: test_deployment.py reuses this exact smoke pass internally
        # and must not insert a second "post_deploy_smoke" suite_run row
        # under the same qualification run.
        target = target_url.rstrip("/")
        suite_id = f"suite-{uuid.uuid4().hex}"
        started_suite = time.time()
        self.conn.execute("""INSERT INTO qa_suite_runs(id,qualification_run_id,suite_key,layer,required,status,
          started_at,command_json) VALUES(?,?,?,'smoke',1,'RUNNING',?,'[]')""", (suite_id, run_id, suite_key, now()))
        self.conn.commit()
        results: list[dict[str, Any]] = []

        def scenario(key, fn):
            result = self._scenario(suite_id, run_id, key, fn)
            results.append(result)
            return result

        def health():
            r = self.http.get(f"{target}/api/system/health", timeout=10)
            if r.status_code != 200 or not r.json().get("ok"):
                raise AssertionError(f"/api/system/health: {r.status_code} {r.text[:300]}")
            return {"status": r.json().get("status")}
        scenario("post_deploy_smoke.health", health)

        def readiness_identity():
            r = self.http.get(f"{target}/api/system/ready", timeout=10)
            body = r.json()
            if r.status_code != 200 or not body.get("ok"):
                raise AssertionError(f"/api/system/ready: {r.status_code} {r.text[:300]}")
            required = {"version", "migration_head", "environment"}
            if not required.issubset(body):
                raise AssertionError(f"/api/system/ready missing fields: {required - set(body)}")
            return {"version": body.get("version"), "migration_head": body.get("migration_head"),
                    "environment": body.get("environment")}
        scenario("post_deploy_smoke.readiness_and_system_identity", readiness_identity)

        def login():
            # MESFlow's /api/auth/login expects a JSON body (request.get_json()) --
            # form-encoded `data=` was a real bug here: it silently produced
            # INVALID_CREDENTIALS on every login attempt, found by hand
            # comparing against LiveMESFlowQualification.login()'s working json= call.
            r = self.http.post(f"{target}/api/auth/login", json={"username": "admin", "password": admin_password}, timeout=10)
            if r.status_code != 200 or not r.json().get("ok"):
                raise AssertionError(f"login failed: {r.status_code} {r.text[:300]}")
            return {"authenticated": True}
        scenario("post_deploy_smoke.login_auth", login)

        def db_connectivity():
            # No direct DB access from here on purpose -- an authenticated
            # read that can only succeed if the app's DB connection pool is
            # actually alive is a more honest "DB connectivity" proof than
            # a bare TCP probe would be.
            r = self.http.get(f"{target}/api/employees?limit=1", timeout=10)
            if r.status_code != 200:
                raise AssertionError(f"DB-backed read failed: {r.status_code} {r.text[:300]}")
            return {"db_backed_read_ok": True}
        scenario("post_deploy_smoke.db_connectivity_via_authenticated_read", db_connectivity)

        def read_po():
            r = self.http.get(f"{target}/api/production-orders?limit=5", timeout=10)
            if r.status_code != 200 or "items" not in r.json():
                raise AssertionError(f"/api/production-orders: {r.status_code} {r.text[:300]}")
            return {"count": len(r.json()["items"])}
        scenario("post_deploy_smoke.read_production_orders", read_po)

        def read_employee():
            r = self.http.get(f"{target}/api/employees?limit=5", timeout=10)
            if r.status_code != 200 or "items" not in r.json():
                raise AssertionError(f"/api/employees: {r.status_code} {r.text[:300]}")
            return {"count": len(r.json()["items"])}
        scenario("post_deploy_smoke.read_employees", read_employee)

        def dashboard_read():
            r = self.http.get(f"{target}/api/dashboard/overview", timeout=10)
            if r.status_code != 200:
                raise AssertionError(f"/api/dashboard/overview: {r.status_code} {r.text[:300]}")
            return {"ok": True}
        scenario("post_deploy_smoke.dashboard_report_read", dashboard_read)

        def kiosk_critical_read():
            r = self.http.get(f"{target}/api/kiosk/v2/health", timeout=10)
            if r.status_code != 200 or not r.json().get("ok"):
                raise AssertionError(f"/api/kiosk/v2/health: {r.status_code} {r.text[:300]}")
            return {"kiosk_backend_ok": True}
        scenario("post_deploy_smoke.kiosk_critical_api_read", kiosk_critical_read)

        if allow_disposable_workflow:
            def disposable_workflow():
                emp = self.http.get(f"{target}/api/employees?limit=200", timeout=10).json().get("items", [])
                emp_id = next((e["id"] for e in emp if str(e.get("employee_no", "")).startswith("QA-")), None)
                ops = self.http.get(f"{target}/api/operations?limit=200", timeout=10).json().get("items", [])
                # Must be the fixture's known-IN_PROGRESS operation
                # specifically -- "any QA-prefixed operation" once picked
                # QA-PO-DONE-OP (a real fixture row that starts already
                # COMPLETED) and got a genuine 409 BUSINESS_CONFLICT,
                # unrelated to whatever this smoke pass was meant to prove.
                op_id = next((o["id"] for o in ops if o.get("code") == "QA-PO-RUN-OP"), None)
                if emp_id is None or op_id is None:
                    return {"skipped": "no QA-fixture employee/operation available on this deployment; "
                                       "read-only checks above already ran"}
                request_id = f"QA-SMOKE-{uuid.uuid4().hex[:12]}"
                start = self.http.post(f"{target}/api/work-sessions/start",
                    json={"employee_id": emp_id, "operation_id": op_id, "request_id": request_id}, timeout=10)
                if start.status_code != 201:
                    raise AssertionError(f"disposable smoke session failed to start: {start.status_code} {start.text[:300]}")
                session_id = start.json()["session"]["id"]
                finish = self.http.post(f"{target}/api/work-sessions/{session_id}/finish",
                    json={"good_qty": 0, "defect_qty": 0, "request_id": request_id + "-FINISH"}, timeout=10)
                if finish.status_code != 200:
                    raise AssertionError(f"disposable smoke session failed to finish: {finish.status_code} {finish.text[:300]}")
                return {"disposable_session_id": session_id, "good_qty": 0}
            scenario("post_deploy_smoke.disposable_workflow_zero_quantity", disposable_workflow)

        status = "FAILED" if any(r["status"] == "FAILED" for r in results) else "PASSED"
        self.conn.execute("UPDATE qa_suite_runs SET status=?,finished_at=? WHERE id=?", (status, now(), suite_id))
        self.conn.commit()
        return {"suite_id": suite_id, "status": status, "scenarios": results,
               "total_duration_seconds": round(time.time() - started_suite, 3)}
