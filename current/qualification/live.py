from __future__ import annotations

import hashlib
import json
import re
import subprocess
import uuid
from typing import Any, Callable

import requests

from .database import DeterministicDatabase
from .evidence import EvidenceStore
from .integrity_runner import IntegrityRunner
from .kiosk_emulator import KioskV2Client
from .store import connect, now


def normalized_fingerprint(scenario: str, step: str, endpoint: str, error_class: str, actual: Any) -> str:
    value = json.dumps(actual, sort_keys=True, default=str)
    value = re.sub(r"\b[0-9a-f]{8,64}\b", "<id>", value, flags=re.I)
    value = re.sub(r"\d{4}-\d\d-\d\dT[^\s\"']+", "<timestamp>", value)
    value = re.sub(r"127\.0\.0\.1:\d+", "127.0.0.1:<port>", value)
    return hashlib.sha256(f"{scenario}|{step}|{endpoint}|{error_class}|{value}".encode()).hexdigest()


class LiveMESFlowQualification:
    def __init__(self, run_id: str, target_url: str, db_container: str, evidence_root, *,
                 intentional_wrong_quantity: bool = False, app_container: str = ""):
        self.run_id = run_id
        self.target = target_url.rstrip("/")
        self.db_container = db_container
        self.app_container = app_container
        self.conn = connect()
        self.evidence = EvidenceStore(evidence_root)
        self.database = DeterministicDatabase(evidence_root)
        self._integrity = IntegrityRunner(evidence_root)
        self.http = requests.Session()
        self.intentional_wrong_quantity = intentional_wrong_quantity
        self.calls: list[dict[str, Any]] = []

    def exec_in_app(self, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
        """Runs a command inside the deployment's own app container (e.g.
        the exact `python -m mesflow.cli reconcile-exceptions` a real
        production cron entry would run) -- requires app_container to have
        been supplied at construction; scenarios needing this must check
        self.app_container first and report BLOCKED rather than raise a
        confusing AttributeError-style failure when it's empty."""
        if not self.app_container:
            raise AssertionError("app_container was not supplied to this LiveMESFlowQualification instance")
        return subprocess.run(["docker", "exec", self.app_container, *args],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)

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
            ("sessions.lifecycle.api_contract", "/api/work-sessions", {"ok", "items"}),
            ("sessions.group_supervisor.api_contract", "/api/work-sessions", {"ok", "items"}),
            ("health.jobs_notifications.api_contract", "/api/system/health", {"ok"}),
            ("kiosk.web_legacy_v2.api_contract", "/api/kiosk-web/health", {"ok"}),
            ("imports.exports.qr.api_contract", "/api/qr-labels", {"ok"}),
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

        def scheduling_dependency_satisfied():
            # Positive-path complement to integration()'s dependency-gate
            # rejection: once the predecessor is genuinely COMPLETED, the
            # downstream operation becomes actionable and a full real
            # start->finish workflow succeeds. A fresh, fully disposable
            # upstream/downstream pair (never QA-PO-RUN-OP) so this can
            # never disturb po_progress()'s exact cumulative assertions above.
            base = self.database.query_json(self.db_container,
                "SELECT id part_id, production_order_id po_id FROM operations WHERE code='QA-PO-LATE-OP'")[0]
            upstream_code = f"QA-SCHED-UP-{uuid.uuid4().hex[:8]}"
            downstream_code = f"QA-SCHED-DOWN-{uuid.uuid4().hex[:8]}"
            self.database._psql(self.db_container, f"""
                INSERT INTO operations(production_order_id,part_id,code,name,done_qty,defect_qty,rework_qty,status,sort_order,qr)
                VALUES({base['po_id']},{base['part_id']},'{upstream_code}','Sched Upstream',5,0,0,'COMPLETED',3,'WF|OP|{upstream_code}');
            """)
            upstream_id = self.database.query_json(self.db_container, f"SELECT id FROM operations WHERE code='{upstream_code}'")[0]["id"]
            self.database._psql(self.db_container, f"""
                INSERT INTO operations(production_order_id,part_id,code,name,done_qty,defect_qty,rework_qty,status,sort_order,qr,predecessor_operation_id)
                VALUES({base['po_id']},{base['part_id']},'{downstream_code}','Sched Downstream',0,0,0,'PLANNED',4,'WF|OP|{downstream_code}',{upstream_id});
            """)
            downstream_id = self.database.query_json(self.db_container, f"SELECT id FROM operations WHERE code='{downstream_code}'")[0]["id"]
            request_id = f"QA-SCHED-OK-{uuid.uuid4().hex[:8]}"
            start = self.request("POST", "/api/work-sessions/start",
                json={"employee_id": ids["employee2"], "operation_id": downstream_id, "request_id": request_id})
            assert start.status_code == 201, f"dependency-satisfied downstream op was still rejected: {start.status_code} {start.text}"
            finish = self.request("POST", f"/api/work-sessions/{start.json()['session']['id']}/finish",
                json={"good_qty": 3, "request_id": request_id + "-FINISH"})
            assert finish.status_code == 200 and finish.json()["session"]["status"] == "CLOSED"
            return {"upstream_operation_id": upstream_id, "downstream_operation_id": downstream_id,
                    "session_id": start.json()["session"]["id"]}
        results.append(self._scenario(suite, "scheduling.dependencies_wip.workflow", "API", scheduling_dependency_satisfied))

        def calendar_shifts_workflow():
            shift = self.database.query_json(self.db_container, "SELECT id,code FROM work_shifts WHERE code='DAY'")[0]
            dashboard = self.request("GET", "/api/dashboard/shift", params={"shift_id": shift["id"]})
            assert dashboard.status_code == 200 and dashboard.json().get("ok"), dashboard.text
            return {"shift_id": shift["id"], "shift_code": shift["code"], "dashboard_ok": True}
        results.append(self._scenario(suite, "calendar.shifts.workflow", "API", calendar_shifts_workflow))

        def kiosk_legacy_workflow():
            operation = self.database.query_json(self.db_container,
                "SELECT id,qr FROM operations WHERE code='QA-PO-LATE-OP'")[0]
            employee = self.database.query_json(self.db_container,
                "SELECT id,qr FROM employees WHERE employee_no='QA-EMP-002'")[0]
            scan_emp = self.request("POST", "/api/kiosk-web/scan", json={"qr": employee["qr"]})
            scan_op = self.request("POST", "/api/kiosk-web/scan", json={"qr": operation["qr"]})
            assert scan_emp.status_code == 200 and scan_op.status_code == 200, (scan_emp.text, scan_op.text)
            request_id = f"WEB-LEGACY-{uuid.uuid4().hex[:8]}"
            start = self.request("POST", "/api/kiosk-web/start",
                json={"employee_id": employee["id"], "operation_id": operation["id"], "request_id": request_id})
            assert start.status_code == 201 and start.json().get("ok"), start.text
            session_id = start.json()["session"]["id"]
            finish = self.request("POST", f"/api/kiosk-web/finish/{session_id}",
                json={"good_qty": 2, "defect_qty": 0, "request_id": request_id + "-FINISH"})
            assert finish.status_code == 200 and finish.json().get("ok"), finish.text
            return {"session_id": session_id, "legacy_protocol_workflow": "scan->scan->start->finish", "finished": True}
        results.append(self._scenario(suite, "kiosk.web_legacy_v2.workflow", "API", kiosk_legacy_workflow))

        def kiosk_offline_recovery_workflow():
            # Real found-by-hand bug the first time this ran: missing the
            # employee RE-scan between the operation scan (opens the
            # session) and the quantity submit (closes it) -- the v2
            # protocol requires SESSION_ACTIVE -> (re-scan employee) ->
            # QUANTITY_INPUT -> quantity before a close is accepted (see
            # kiosk_emulator.py's own session_close_normal_quantity for the
            # same sequence). Without it the quantity submit was silently
            # REJECTED (HTTP 200, accepted:false) -- and this scenario's own
            # check only looked at the HTTP-level replay status, never at
            # `accepted`, so it reported success while leaving QA-EMP-002's
            # session open. That dangling session then broke unrelated
            # LATER kiosk_emulator.py scenarios reusing the same employee,
            # with a BUSINESS_CONFLICT that took real cross-suite debugging
            # to trace back here.
            client = KioskV2Client(self.http, self.target, f"qa-wf-kiosk-{uuid.uuid4().hex[:8]}")
            client.bootstrap()
            client.go_offline()
            client.scan("WF|EMP|QA-EMP-002")
            op_qr = self.database.query_json(self.db_container, "SELECT qr FROM operations WHERE code='QA-PO-LATE-OP'")[0]["qr"]
            client.scan(op_qr)
            client.scan("WF|EMP|QA-EMP-002")  # re-scan -> QUANTITY_INPUT
            client.quantity(good=1, defect=0, rework=0)
            replayed = client.reconnect_and_replay()
            not_accepted = [(status, body) for status, body in replayed if status != "PASSED" or body.get("accepted") is False]
            if not_accepted or not replayed:
                raise AssertionError(f"offline-queued business events did not all replay as genuinely accepted: {replayed}")
            final_state = replayed[-1][1]["state"]["name"]
            if final_state != "WAIT_EMPLOYEE":
                raise AssertionError(f"session did not actually close after the replayed quantity submit: final_state={final_state}")
            open_sessions = self.database.query_json(self.db_container,
                "SELECT count(*) n FROM work_sessions ws JOIN employees e ON e.id=ws.employee_id "
                "WHERE e.employee_no='QA-EMP-002' AND ws.status='OPEN'")[0]["n"]
            if open_sessions:
                raise AssertionError(f"QA-EMP-002 has {open_sessions} dangling OPEN session(s) after this scenario -- would break later suites")
            return {"queued_event_count": len(replayed), "final_state": final_state}
        results.append(self._scenario(suite, "kiosk.offline_recovery.workflow", "API", kiosk_offline_recovery_workflow))

        def imports_exports_qr_workflow():
            response = self.request("GET", "/api/operations/export.xlsx")
            assert response.status_code == 200, response.text
            content_type = response.headers.get("Content-Type", "")
            assert "spreadsheet" in content_type or "excel" in content_type.lower(), f"unexpected Content-Type: {content_type}"
            assert len(response.content) > 0, "export.xlsx returned an empty body"
            return {"content_type": content_type, "body_bytes": len(response.content)}
        results.append(self._scenario(suite, "imports.exports.qr.workflow", "API", imports_exports_qr_workflow))

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

        def auth_integration():
            me = self.request("GET", "/api/auth/me").json()
            assert me.get("ok") and me["user"]["role"] == "admin" and me["user"]["permissions"]
            audit = self.database.query_json(self.db_container,
                "SELECT count(*) n FROM audit_logs WHERE action='LOGIN_SUCCESS'")[0]["n"]
            assert audit >= 1, "no LOGIN_SUCCESS row persisted in audit_logs after a real login"
            return {"role": me["user"]["role"], "permission_count": len(me["user"]["permissions"]),
                    "login_audit_rows": audit}
        values.append(self._scenario(suite, "auth.sessions_roles.integration", "API", auth_integration))

        def scheduling_dependency_gate():
            # Ad hoc, scenario-scoped operation with a REAL predecessor
            # dependency (spec: "scheduling.dependencies_wip") -- created
            # here rather than in the shared fixture SQL, since the fixture
            # currently has no operation with a configured
            # predecessor_operation_id at all, and every other suite's
            # exact-count assertions against the shared fixture rows must
            # stay untouched. QA-PO-RUN-OP (the predecessor) is 'IN_PROGRESS'
            # with done_qty=0, so a downstream operation depending on it must
            # be rejected as not-yet-actionable -- the real, enforced
            # execution.py business rule, not a QA-only simulation of it.
            ids = self._ids()
            upstream_id = ids["operation_id"]
            downstream_code = f"QA-SCHED-DEP-{uuid.uuid4().hex[:8]}"
            self.database._psql(self.db_container, f"""
                INSERT INTO operations(production_order_id,part_id,code,name,done_qty,defect_qty,rework_qty,status,sort_order,qr,predecessor_operation_id)
                SELECT production_order_id,part_id,'{downstream_code}','Scheduling Dependency Test Op',0,0,0,'PLANNED',2,'WF|OP|{downstream_code}',{upstream_id}
                FROM operations WHERE id={upstream_id};
            """)
            downstream_id = self.database.query_json(self.db_container,
                f"SELECT id FROM operations WHERE code='{downstream_code}'")[0]["id"]
            response = self.request("POST", "/api/work-sessions/start",
                json={"employee_id": ids["employee2"], "operation_id": downstream_id,
                      "request_id": f"QA-SCHED-{uuid.uuid4().hex[:8]}"})
            if response.status_code != 409:
                raise AssertionError(f"expected 409 dependency-gated rejection, got {response.status_code} {response.text}")
            no_session = self.database.query_json(self.db_container,
                f"SELECT count(*) n FROM work_sessions WHERE operation_id={downstream_id}")[0]["n"]
            assert no_session == 0, "a session was created despite the dependency gate rejecting the start"
            return {"downstream_operation_id": downstream_id, "predecessor_operation_id": upstream_id,
                    "rejection_status": response.status_code, "rejection_body": response.json()}
        values.append(self._scenario(suite, "scheduling.dependencies_wip.integration", "API", scheduling_dependency_gate))

        def calendar_shifts_integration():
            shifts = self.database.query_json(self.db_container, "SELECT count(*) n FROM work_shifts")[0]["n"]
            intervals = self.database.query_json(self.db_container, "SELECT count(*) n FROM work_shift_intervals")[0]["n"]
            assert shifts >= 2, f"expected the migration-seeded DAY/NIGHT shifts, found {shifts}"
            assert intervals >= 2, f"expected real work/break intervals, found {intervals}"
            api_shifts = self.request("GET", "/api/settings/working-calendar").json()
            assert api_shifts.get("ok") and len(api_shifts.get("shifts", [])) == shifts, \
                f"API-reported shift count ({len(api_shifts.get('shifts', []))}) disagrees with DB ({shifts})"
            return {"db_shift_count": shifts, "db_interval_count": intervals, "api_shift_count": len(api_shifts["shifts"])}
        values.append(self._scenario(suite, "calendar.shifts.integration", "API", calendar_shifts_integration))

        def quality_rework_integration():
            # QA-PO-LATE-OP, NOT ids['operation_id'] (QA-PO-RUN-OP) --
            # workflows()'s po_progress() asserts EXACT cumulative done_qty/
            # defect_qty/rework_qty on QA-PO-RUN-OP across its own fixed
            # scenario sequence; touching it here (integration() runs
            # BEFORE workflows() in the CLI's suite order) silently
            # corrupted those exact-value assertions -- a real bug found by
            # hand the first time this scenario ran for real.
            employee_id = self.database.query_json(self.db_container,
                "SELECT id FROM employees WHERE employee_no='QA-EMP-002'")[0]["id"]
            operation_id = self.database.query_json(self.db_container,
                "SELECT id FROM operations WHERE code='QA-PO-LATE-OP'")[0]["id"]
            request_id = f"QA-INTEG-REWORK-{uuid.uuid4().hex[:8]}"
            start = self.request("POST", "/api/work-sessions/start",
                json={"employee_id": employee_id, "operation_id": operation_id, "request_id": request_id})
            assert start.status_code == 201, start.text
            sid = start.json()["session"]["id"]
            finish = self.request("POST", f"/api/work-sessions/{sid}/finish",
                json={"good_qty": 1, "defect_qty": 2, "rework_qty": 2, "request_id": request_id + "-FINISH"})
            assert finish.status_code == 200
            movements = self.database.query_json(self.db_container,
                f"SELECT movement_type,delta FROM quantity_movements WHERE session_id={sid} ORDER BY movement_type")
            assert movements, "no quantity_movements rows persisted for a session with real defect/rework quantities"
            return {"session_id": sid, "movements": movements}
        values.append(self._scenario(suite, "quality.quantities_rework.integration", "API", quality_rework_integration))

        def kiosk_legacy_integration():
            emp = self.database.query_json(self.db_container,
                "SELECT qr FROM employees WHERE employee_no='QA-EMP-002'")[0]["qr"]
            op = self.database.query_json(self.db_container,
                "SELECT qr FROM operations WHERE code='QA-PO-RUN-OP'")[0]["qr"]
            scan1 = self.request("POST", "/api/kiosk-web/scan", json={"qr": emp})
            assert scan1.status_code == 200 and scan1.json().get("ok") and scan1.json().get("type") == "employee", scan1.text
            scan2 = self.request("POST", "/api/kiosk-web/scan", json={"qr": op})
            assert scan2.status_code == 200 and scan2.json().get("ok") and scan2.json().get("type") == "operation", scan2.text
            return {"employee_lookup": scan1.json()["employee"]["employee_no"], "operation_lookup": scan2.json()["operation"]["code"]}
        values.append(self._scenario(suite, "kiosk.web_legacy_v2.integration", "API", kiosk_legacy_integration))

        def kiosk_offline_recovery_integration():
            client = KioskV2Client(self.http, self.target, f"qa-integ-kiosk-{uuid.uuid4().hex[:8]}")
            client.bootstrap()
            client.go_offline()
            client.scan("WF|EMP|QA-EMP-001")
            replayed = client.reconnect_and_replay()
            if not replayed or replayed[0][0] != "PASSED":
                raise AssertionError(f"queued offline event did not replay successfully: {replayed}")
            return {"queued_and_replayed": len(replayed), "final_state": replayed[-1][1]["state"]["name"]}
        values.append(self._scenario(suite, "kiosk.offline_recovery.integration", "API", kiosk_offline_recovery_integration))

        def exceptions_reconciliation_integration():
            listing = self.request("GET", "/api/exceptions").json()
            assert listing.get("ok"), listing
            table_exists = self.database.query_json(self.db_container,
                "SELECT count(*) n FROM information_schema.tables WHERE table_name='exception_records'")[0]["n"]
            assert table_exists == 1, "exception_records table is missing"
            return {"listing_ok": True, "exception_records_table_present": True}
        values.append(self._scenario(suite, "exceptions.reconciliation.integration", "API", exceptions_reconciliation_integration))

        def health_jobs_integration():
            jobs = self.database.query_json(self.db_container,
                "SELECT job_name,last_status FROM scheduled_job_health ORDER BY job_name")
            assert jobs, "scheduled_job_health has no rows -- exception_reconciliation/shift_session_reconciliation jobs are not registered"
            names = {row["job_name"] for row in jobs}
            assert {"exception_reconciliation", "shift_session_reconciliation"}.issubset(names), \
                f"expected both real scheduled jobs registered, found {names}"
            return {"jobs": jobs}
        values.append(self._scenario(suite, "health.jobs_notifications.integration", "API", health_jobs_integration))

        def imports_exports_qr_integration():
            labels = self.request("GET", "/api/qr-labels", params={"type": "EMPLOYEE"}).json()
            assert labels.get("ok") and labels.get("items"), labels
            employee_count = self.database.query_json(self.db_container, "SELECT count(*) n FROM employees")[0]["n"]
            assert len(labels["items"]) == employee_count, \
                f"QR label catalogue returned {len(labels['items'])} employees, DB has {employee_count}"
            return {"qr_label_count": len(labels["items"]), "employee_count": employee_count}
        values.append(self._scenario(suite, "imports.exports.qr.integration", "API", imports_exports_qr_integration))

        self._finish_suite(suite)
        return values

    def invariants(self) -> dict[str, Any]:
        # Delegates to the shared IntegrityRunner (spec: "Integrity Runner
        # consolidation") -- this was the fullest/authoritative one of what
        # were three independently hand-rolled copies of this same check
        # set (recovery.py and load_soak.py had their own, smaller,
        # never-persisted versions; both now delegate here too). Kept as a
        # thin wrapper, not removed, so every existing caller of
        # LiveMESFlowQualification.invariants() keeps working unchanged.
        return self._integrity.check(self.run_id, self.db_container, evidence_name="domain-invariants")

    def _finish_suite(self, suite_id: str) -> None:
        statuses = [r["status"] for r in self.conn.execute("SELECT status FROM qa_scenario_runs WHERE suite_run_id=?", (suite_id,))]
        status = "FAILED" if "FAILED" in statuses else "PASSED"
        self.conn.execute("UPDATE qa_suite_runs SET status=?,finished_at=? WHERE id=?", (status, now(), suite_id))
        self.conn.commit()
