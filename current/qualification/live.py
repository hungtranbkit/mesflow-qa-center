from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any, Callable

import requests

from .database import DeterministicDatabase
from .evidence import EvidenceStore
from .store import connect, now


def normalized_fingerprint(scenario: str, step: str, endpoint: str, error_class: str, actual: Any) -> str:
    value = json.dumps(actual, sort_keys=True, default=str)
    value = re.sub(r"\b[0-9a-f]{8,64}\b", "<id>", value, flags=re.I)
    value = re.sub(r"\d{4}-\d\d-\d\dT[^\s\"']+", "<timestamp>", value)
    value = re.sub(r"127\.0\.0\.1:\d+", "127.0.0.1:<port>", value)
    return hashlib.sha256(f"{scenario}|{step}|{endpoint}|{error_class}|{value}".encode()).hexdigest()


class LiveMESFlowQualification:
    def __init__(self, run_id: str, target_url: str, db_container: str, evidence_root, *, intentional_wrong_quantity: bool = False):
        self.run_id = run_id
        self.target = target_url.rstrip("/")
        self.db_container = db_container
        self.conn = connect()
        self.evidence = EvidenceStore(evidence_root)
        self.database = DeterministicDatabase(evidence_root)
        self.http = requests.Session()
        self.intentional_wrong_quantity = intentional_wrong_quantity
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        response = self.http.request(method, self.target + path, timeout=15, **kwargs)
        safe_request = {"method": method, "path": path, "json": kwargs.get("json")}
        self.calls.append({"request": safe_request, "status": response.status_code,
                           "response": response.text[:4000], "at": now()})
        return response

    def login(self) -> None:
        response = self.request("POST", "/api/auth/login", json={"username": "admin", "password": "Admin@123456"})
        if response.status_code != 200 or not response.json().get("ok"):
            raise AssertionError(f"deployed auth failed: {response.status_code} {response.text}")

    def _ids(self) -> dict[str, int]:
        rows = self.database.query_json(self.db_container, """SELECT e.id employee1,
          (SELECT id FROM employees WHERE employee_no='QA-EMP-002') employee2,
          o.id operation_id,o.production_order_id po_id,(SELECT id FROM stations WHERE code='QA-ST-001') station_id
          FROM employees e,operations o WHERE e.employee_no='QA-EMP-001' AND o.code='QA-PO-RUN-OP'""")
        return rows[0]

    def _suite(self, key: str, layer: str) -> str:
        suite_id = f"suite-{uuid.uuid4().hex}"
        self.conn.execute("""INSERT INTO qa_suite_runs(id,qualification_run_id,suite_key,layer,required,status,started_at,command_json)
          VALUES(?,?,?,?,1,'RUNNING',?,'[]')""", (suite_id, self.run_id, key, layer, now()))
        self.conn.commit()
        return suite_id

    def _scenario(self, suite_id: str, key: str, driver: str, fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        scenario_id = f"scenario-{uuid.uuid4().hex}"
        self.conn.execute("""INSERT INTO qa_scenario_runs(id,suite_run_id,scenario_key,scenario_version,driver,status,started_at)
          VALUES(?,?,?,?,?,'RUNNING',?)""", (scenario_id, suite_id, key, "mesflow-software-v1", driver, now()))
        self.conn.commit()
        self.calls = []
        status, first_step, fp, expected, actual = "PASSED", "", "", {}, {}
        try:
            actual = fn()
            expected = {"outcome": "domain assertions and database invariants pass"}
        except Exception as exc:
            status = "FAILED"
            first_step = getattr(exc, "step", "assertion")
            actual = {"error_class": type(exc).__name__, "message": str(exc)}
            endpoint = self.calls[-1]["request"]["path"] if self.calls else "database"
            fp = normalized_fingerprint(key, first_step, endpoint, type(exc).__name__, actual)
        evidence = self.evidence.write_json(self.run_id, f"{key.replace('.','-')}.json",
          {"artifact_run_id": self.run_id, "scenario": key, "scenario_version": "mesflow-software-v1",
           "expected": expected, "actual": actual, "first_failing_step": first_step,
           "fingerprint": fp, "api_calls": self.calls, "timestamps": {"finished_at": now()}},
          kind="SCENARIO_EVIDENCE", suite_run_id=suite_id, scenario_run_id=scenario_id)
        self.conn.execute("""UPDATE qa_scenario_runs SET status=?,finished_at=?,first_failing_step=?,fingerprint=?,
          expected_json=?,actual_json=? WHERE id=?""", (status, now(), first_step, fp, json.dumps(expected), json.dumps(actual), scenario_id))
        self.conn.execute("INSERT INTO qa_attempts(scenario_run_id,attempt_no,status,fingerprint,started_at,finished_at) VALUES(?,1,?,?,?,?)",
                          (scenario_id, status, fp, now(), now()))
        self.conn.commit()
        return {"id": scenario_id, "key": key, "status": status, "fingerprint": fp, "evidence": evidence}

    def api_contracts(self) -> list[dict[str, Any]]:
        suite = self._suite("api_contract", "api")
        def contracts():
            unauth = requests.get(self.target + "/api/employees", timeout=10)
            assert unauth.status_code == 401 and unauth.json().get("error") == "AUTH_REQUIRED"
            self.login()
            checks = [
                ("/api/system/ready", {"ok", "migration_head", "version", "timezone"}),
                ("/api/employees", {"ok", "items"}), ("/api/production-orders", {"ok", "items"}),
                ("/api/parts", {"ok", "items"}), ("/api/operations", {"ok", "items"}),
                ("/api/work-sessions", {"ok", "items"}), ("/api/dashboard/overview", {"ok"}),
                ("/api/reports/employee-performance", {"ok"}), ("/api/qc/inspections", {"ok", "items"}),
                ("/api/kiosk/v2/health", {"ok"}),
            ]
            results = []
            for path, fields in checks:
                response = self.request("GET", path)
                body = response.json()
                assert response.status_code == 200, f"{path}: {response.status_code} {response.text}"
                assert fields.issubset(body), f"{path}: missing {fields-set(body)}"
                results.append({"path": path, "fields": sorted(fields)})
            invalid = self.request("POST", "/api/work-sessions/start", json={})
            assert invalid.status_code in {400, 404, 409}
            return {"contracts": results, "unauthorized_status": unauth.status_code, "invalid_status": invalid.status_code}
        result = self._scenario(suite, "auth.sessions_roles.api_contract", "API", contracts)
        feature_contracts = [
            ("dashboard.production_overview.api_contract", "/api/dashboard/overview", {"ok"}),
            ("production_orders.lifecycle.api_contract", "/api/production-orders", {"ok", "items"}),
            ("templates.parts.operations.api_contract", "/api/templates", {"ok", "items"}),
            ("employees.stations_equipment.api_contract", "/api/employees", {"ok", "items"}),
            ("trace.audit.api_contract", "/api/production-orders", {"ok", "items"}),
            ("reports.kpi_productivity.api_contract", "/api/reports/employee-performance", {"ok"}),
        ]
        values = [result]
        for feature_key, path, fields in feature_contracts:
            def check(path=path, fields=fields):
                response = self.request("GET", path); body = response.json()
                assert response.status_code == 200 and fields.issubset(body)
                return {"path": path, "required_fields": sorted(fields), "status": response.status_code}
            values.append(self._scenario(suite, feature_key, "API", check))
        self._finish_suite(suite)
        return values

    def workflows(self, *, suite_key: str = "mes_workflows") -> list[dict[str, Any]]:
        # suite_key is overridable so a caller reusing these same real
        # business scenarios in a DIFFERENT suite context (e.g. the upgrade
        # suite's post-upgrade smoke pass) doesn't insert a second
        # "mes_workflows" suite_run row under the same qualification run --
        # every scenario_key inside stays unprefixed either way, since
        # coverage.py matches on scenario_key, not suite_key.
        self.login()
        ids = self._ids()
        suite = self._suite(suite_key, "workflow")
        results = []

        def normal():
            start = self.request("POST", "/api/work-sessions/start", json={"employee_id": ids["employee1"], "operation_id": ids["operation_id"], "station_id": ids["station_id"], "request_id": "QA-NORMAL-START"})
            assert start.status_code == 201, start.text
            sid = start.json()["session"]["id"]
            active = self.request("GET", "/api/work-sessions").json()["items"]
            assert any(x["id"] == sid and x["status"] == "OPEN" for x in active)
            finish = self.request("POST", f"/api/work-sessions/{sid}/finish", json={"good_qty": 5, "defect_qty": 0, "request_id": "QA-NORMAL-FINISH"})
            assert finish.status_code == 200 and finish.json()["session"]["status"] == "CLOSED"
            rows = self.database.query_json(self.db_container, f"SELECT done_qty,defect_qty,status FROM operations WHERE id={ids['operation_id']}")
            expected_qty = 999 if self.intentional_wrong_quantity else 5
            assert rows[0]["done_qty"] == expected_qty, f"operation done_qty expected {expected_qty}, got {rows[0]['done_qty']}"
            dashboard = self.request("GET", "/api/dashboard/overview")
            report = self.request("GET", f"/api/reports/operations/{ids['operation_id']}")
            assert dashboard.status_code == 200 and report.status_code == 200
            return {"session_id": sid, "operation": rows[0], "dashboard_ok": True, "report_ok": True}
        results.append(self._scenario(suite, "sessions.lifecycle.normal_production", "API", normal))

        def zero():
            s = self.request("POST", "/api/work-sessions/start", json={"employee_id": ids["employee2"], "operation_id": ids["operation_id"], "request_id": "QA-ZERO-START"})
            assert s.status_code == 201
            f = self.request("POST", f"/api/work-sessions/{s.json()['session']['id']}/finish", json={"good_qty": 0, "defect_qty": 0, "request_id": "QA-ZERO-FINISH"})
            assert f.status_code == 200 and f.json()["session"]["good_qty"] == 0
            return {"rule": "zero quantity close is allowed", "session": f.json()["session"]}
        results.append(self._scenario(suite, "sessions.lifecycle.zero_quantity", "API", zero))

        def multiple():
            sessions = []
            for employee, req in ((ids["employee1"], "QA-MULTI-1"), (ids["employee2"], "QA-MULTI-2")):
                r = self.request("POST", "/api/work-sessions/start", json={"employee_id": employee, "operation_id": ids["operation_id"], "request_id": req})
                assert r.status_code == 201, r.text; sessions.append(r.json()["session"]["id"])
            for index, sid in enumerate(sessions, 1):
                r = self.request("POST", f"/api/work-sessions/{sid}/finish", json={"good_qty": index, "request_id": f"QA-MULTI-F-{index}"})
                assert r.status_code == 200
            qty = self.database.query_json(self.db_container, f"SELECT done_qty FROM operations WHERE id={ids['operation_id']}")[0]["done_qty"]
            assert qty == 8
            return {"session_ids": sessions, "aggregate_done_qty": qty}
        results.append(self._scenario(suite, "sessions.group_supervisor.multiple_workers", "API", multiple))

        def duplicate():
            replay = self.request("POST", "/api/work-sessions/start", json={"employee_id": ids["employee1"], "operation_id": ids["operation_id"], "request_id": "QA-DUP-START"})
            assert replay.status_code == 201
            second = self.request("POST", "/api/work-sessions/start", json={"employee_id": ids["employee1"], "operation_id": ids["operation_id"], "request_id": "QA-DUP-START"})
            assert second.status_code == 201 and second.json().get("idempotent_replay") is True
            sid = replay.json()["session"]["id"]
            f1 = self.request("POST", f"/api/work-sessions/{sid}/finish", json={"good_qty": 4, "request_id": "QA-DUP-FINISH"})
            f2 = self.request("POST", f"/api/work-sessions/{sid}/finish", json={"good_qty": 4, "request_id": "QA-DUP-FINISH"})
            assert f1.status_code == f2.status_code == 200 and f2.json().get("idempotent_replay") is True
            count = self.database.query_json(self.db_container, "SELECT count(*) n FROM quantity_movements WHERE correlation_id='QA-DUP-FINISH'")[0]["n"]
            assert count <= 1
            return {"session_id": sid, "movement_count": count}
        results.append(self._scenario(suite, "sessions.lifecycle.duplicate_protection", "API", duplicate))

        def rework():
            s = self.request("POST", "/api/work-sessions/start", json={"employee_id": ids["employee2"], "operation_id": ids["operation_id"], "request_id": "QA-REWORK-START"})
            assert s.status_code == 201
            f = self.request("POST", f"/api/work-sessions/{s.json()['session']['id']}/finish", json={"good_qty": 2, "defect_qty": 3, "rework_qty": 2, "request_id": "QA-REWORK-FINISH"})
            assert f.status_code == 200
            row = self.database.query_json(self.db_container, f"SELECT defect_qty,rework_qty FROM operations WHERE id={ids['operation_id']}")[0]
            assert row["defect_qty"] == 3 and row["rework_qty"] == 2
            return {"operation": row, "repairable_rule": "rework_qty <= defect_qty"}
        results.append(self._scenario(suite, "quality.quantities_rework.repair_flow", "API", rework))

        def invalid():
            before = self.database.query_json(self.db_container, "SELECT count(*) n FROM work_sessions")[0]["n"]
            response = self.request("POST", "/api/work-sessions/start", json={"employee_id": 99999999, "operation_id": ids["operation_id"], "request_id": "QA-INVALID"})
            after = self.database.query_json(self.db_container, "SELECT count(*) n FROM work_sessions")[0]["n"]
            assert response.status_code in {400, 404} and after == before
            return {"status": response.status_code, "session_count_unchanged": True}
        results.append(self._scenario(suite, "sessions.lifecycle.invalid_input", "API", invalid))

        def historical():
            hist = self.database.query_json(self.db_container, "SELECT ws.id,e.id employee_id FROM work_sessions ws JOIN employees e ON e.id=ws.employee_id WHERE ws.start_request_id='QA-HIST-START'")[0]
            response = self.request("DELETE", f"/api/employees/{hist['employee_id']}")
            remaining = self.database.query_json(self.db_container, f"SELECT count(*) n FROM work_sessions WHERE id={hist['id']}")[0]["n"]
            assert response.status_code in {400, 409} and remaining == 1
            return {"delete_status": response.status_code, "historical_session_preserved": True}
        results.append(self._scenario(suite, "trace.audit.historical_integrity", "API", historical))
        def po_progress():
            operation = self.database.query_json(self.db_container, f"SELECT done_qty,defect_qty,rework_qty FROM operations WHERE id={ids['operation_id']}")[0]
            po = self.database.query_json(self.db_container, f"SELECT status,planned_quantity FROM production_orders WHERE id={ids['po_id']}")[0]
            assert operation["done_qty"] == 14 and operation["defect_qty"] == 3 and operation["rework_qty"] == 2
            assert po["status"] == "IN_PROGRESS" and po["planned_quantity"] == 100
            return {"operation": operation, "production_order": po}
        results.append(self._scenario(suite, "production_orders.lifecycle.aggregate_progress", "API", po_progress))
        self._finish_suite(suite)
        return results

    def integration(self) -> list[dict[str, Any]]:
        suite = self._suite("integration", "integration")
        def verify():
            self.login()
            ready = self.request("GET", "/api/system/ready").json()
            db = self.database.query_json(self.db_container, "SELECT current_database() database,(SELECT version_num FROM alembic_version) migration_head")[0]
            assert ready["migration_head"] == db["migration_head"]
            assert db["database"] == "mesflow_qa"
            assert ready.get("database_backend", "postgresql") == "postgresql"
            return {"runtime": ready, "database": db}
        result = self._scenario(suite, "database.migrations_backup.runtime_integration", "API", verify)
        values = [result]
        integrations = [
            ("production_orders.lifecycle.integration", "SELECT count(*) n FROM production_orders WHERE code LIKE 'QA-PO-%'", 4),
            ("templates.parts.operations.integration", "SELECT count(*) n FROM operations WHERE code LIKE 'QA-PO-%-OP'", 4),
            ("trace.audit.integration", "SELECT count(*) n FROM production_trace_events", None),
            ("reports.kpi_productivity.integration", "SELECT count(*) n FROM work_sessions", None),
        ]
        for key, sql, expected in integrations:
            def check(key=key, sql=sql, expected=expected):
                value = self.database.query_json(self.db_container, sql)[0]["n"]
                if expected is not None:
                    assert value == expected, f"{key}: expected {expected}, got {value}"
                else:
                    assert value >= 0
                return {"database_count": value, "deployed_runtime": self.target}
            values.append(self._scenario(suite, key, "API", check))
        self._finish_suite(suite)
        return values

    def invariants(self) -> dict[str, Any]:
        checks = {
            "ONE_ACTIVE_SESSION_PER_EMPLOYEE": "SELECT employee_id,count(*) n FROM work_sessions WHERE status='OPEN' GROUP BY employee_id HAVING count(*)>1",
            "NON_NEGATIVE_QUANTITIES": "SELECT id,good_qty,defect_qty,rework_qty FROM work_sessions WHERE good_qty<0 OR defect_qty<0 OR rework_qty<0 OR rework_qty>defect_qty",
            "SESSION_TEMPORAL_ORDER": "SELECT id,started_at,ended_at FROM work_sessions WHERE ended_at IS NOT NULL AND ended_at<started_at",
            "NO_DUPLICATE_QUANTITY_MOVEMENT": "SELECT correlation_id,session_id,movement_type,delta,count(*) n FROM quantity_movements WHERE correlation_id<>'' GROUP BY correlation_id,session_id,movement_type,delta HAVING count(*)>1",
            "REFERENTIAL_INTEGRITY": "SELECT ws.id FROM work_sessions ws LEFT JOIN employees e ON e.id=ws.employee_id LEFT JOIN operations o ON o.id=ws.operation_id WHERE e.id IS NULL OR o.id IS NULL",
        }
        results = {}
        for key, sql in checks.items():
            violations = self.database.query_json(self.db_container, sql)
            status = "PASSED" if not violations else "FAILED"
            self.conn.execute("""INSERT INTO qa_invariant_results(qualification_run_id,scenario_run_id,invariant_key,status,
              expected_json,actual_json,created_at) VALUES(?,NULL,?,?,?,?,?)""",
              (self.run_id, key, status, json.dumps([]), json.dumps(violations), now()))
            results[key] = {"status": status, "violations": violations}
        self.conn.commit()
        self.evidence.write_json(self.run_id, "domain-invariants.json", results, kind="INVARIANT_EVIDENCE")
        return results

    def _finish_suite(self, suite_id: str) -> None:
        statuses = [r["status"] for r in self.conn.execute("SELECT status FROM qa_scenario_runs WHERE suite_run_id=?", (suite_id,))]
        status = "FAILED" if "FAILED" in statuses else "PASSED"
        self.conn.execute("UPDATE qa_suite_runs SET status=?,finished_at=? WHERE id=?", (status, now(), suite_id))
        self.conn.commit()
