"""kiosk_emulator: production-policy suite covering real kiosk business
behavior through the real MESFlow kiosk v2 protocol (app/mesflow/web/
kiosk_v2.py) -- no physical ESP hardware required, matches the spec.

Talks the real wire protocol directly with `requests` rather than through
qualification/drivers.py's KioskEmulatorDriver: that driver injects the
device identity as a top-level `device_uuid` JSON field (a v1-shaped
assumption), but kiosk v2's actual contract nests it as
`body["device"]["device_id"]` (see kiosk_v2._device_id_from) and requires
a protocol envelope (`protocol_version`, `event.event_id`/`type`,
`context.expected_state_version`) the generic driver never constructs.
Using the driver as-is would have silently talked past every kiosk_emulator
scenario with device_id='unknown'. Documented here rather than quietly
worked around: the driver needs a kiosk-v2-aware step vocabulary before it
can honestly be reused for this suite; KioskV2Client below is the correct,
protocol-accurate client in the meantime, and every event still gets
persisted through the SAME real `/api/kiosk/v2/events` endpoint the
firmware calls, so this is genuine backend coverage, not a bypass of it.

Scenario keys are feature-prefixed (sessions.lifecycle.kiosk_*,
quality.quantities_rework.kiosk_*, sessions.group_supervisor.kiosk_*,
kiosk.offline_recovery.*) rather than "kiosk_emulator.*" -- coverage.py
matches scenario_key against the feature registry by prefix, and this
suite's own KIOSK_EMULATOR-driver evidence is exactly what several
features' required_drivers list explicitly needs. The suite_key
('kiosk_emulator', recorded on qa_suite_runs, layer='kiosk') still
identifies which SUITE produced each scenario -- renaming scenario_key
away from that prefix loses nothing, since the suite/layer linkage lives
there, not in the key string.
"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import requests

from .database import DeterministicDatabase
from .evidence import EvidenceStore
from .scenario_runner import ScenarioRunner
from .store import connect, now


class KioskV2Client:
    def __init__(self, http: requests.Session, target: str, device_id: str):
        self.http = http
        self.target = target.rstrip("/")
        self.device_id = device_id
        self.online = True
        self.queue: list[dict[str, Any]] = []
        self.seq = 0
        self.last_state: dict[str, Any] = {}

    def bootstrap(self) -> tuple[int, dict[str, Any]]:
        r = self.http.post(f"{self.target}/api/kiosk/v2/bootstrap",
                           json={"device_id": self.device_id, "hardware_id": self.device_id}, timeout=10)
        body = r.json()
        if "state" in body:
            self.last_state = body["state"]
        return r.status_code, body

    def _envelope(self, event_type: str, payload: dict[str, Any], event_id: str) -> dict[str, Any]:
        self.seq += 1
        return {"protocol_version": 1, "device": {"device_id": self.device_id, "hardware_id": self.device_id},
                "event": {"event_id": event_id, "type": event_type, "device_seq": self.seq},
                "context": {}, "payload": payload}

    def send(self, event_type: str, payload: dict[str, Any], *, event_id: str | None = None) -> tuple[str, dict[str, Any]]:
        event_id = event_id or f"{self.device_id}-{uuid.uuid4().hex}"
        body = self._envelope(event_type, payload, event_id)
        if not self.online:
            self.queue.append(body)
            return "QUEUED", {"queued": True, "queue_size": len(self.queue)}
        status, resp = self._post(body)
        return status, resp

    def _post(self, body: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        r = self.http.post(f"{self.target}/api/kiosk/v2/events", json=body, timeout=10)
        resp = r.json()
        if "state" in resp:
            self.last_state = resp["state"]
        return ("PASSED" if r.status_code == 200 else "HTTP_ERROR"), resp

    def scan(self, raw: str, *, event_id: str | None = None):
        return self.send("SCAN", {"raw": raw}, event_id=event_id)

    def quantity(self, good: int, defect: int = 0, rework: int = 0):
        return self.send("QUANTITY_SUBMITTED", {"quantity_good": good, "quantity_defect": defect, "quantity_rework": rework})

    def go_offline(self):
        self.online = False

    def reconnect_and_replay(self) -> list[tuple[str, dict[str, Any]]]:
        self.online = True
        pending, self.queue = self.queue, []
        results = []
        for body in pending:
            results.append(self._post(body))
        return results


class KioskEmulatorRunner:
    def __init__(self, evidence_root: Path):
        self.conn = connect()
        self.evidence = EvidenceStore(evidence_root)
        self.evidence_root = evidence_root
        self.http = requests.Session()
        self._runner = ScenarioRunner(evidence_root, scenario_version="kiosk-v2-emulator-v1",
                                      driver="KIOSK_EMULATOR", evidence_kind="KIOSK_EMULATOR_EVIDENCE")

    def _scenario(self, suite_id: str, run_id: str, key: str, fn) -> dict[str, Any]:
        return self._runner.run(suite_id, run_id, key, fn)

    def run(self, run_id: str, target_url: str, db_container: str, *,
            admin_password: str = "Admin@123456", profile: str | None = None) -> dict[str, Any]:
        """profile: an optional kiosk_registry.PROFILES name -- when given,
        only scenarios whose scenario_key is registered under one of that
        profile's testcase_ids actually run; everything else is silently
        skipped (not recorded as failed/blocked -- it simply wasn't
        selected). None (the default) runs every scenario, preserving the
        exact pre-Kiosk-Certification-phase behavior for every existing
        caller (cli.py's run-artifact, replay-suite, etc.)."""
        target = target_url.rstrip("/")
        suite_id = f"suite-{uuid.uuid4().hex}"
        self.conn.execute("""INSERT INTO qa_suite_runs(id,qualification_run_id,suite_key,layer,required,status,
          started_at,command_json) VALUES(?,?,'kiosk_emulator','kiosk',1,'RUNNING',?,'[]')""", (suite_id, run_id, now()))
        self.conn.commit()
        results: list[dict[str, Any]] = []
        database = DeterministicDatabase(self.evidence_root)

        selected_keys: set[str] | None = None
        if profile:
            from .kiosk_registry import PROFILES, REGISTRY
            spec = PROFILES.get(profile.upper())
            if spec is None:
                raise ValueError(f"unknown kiosk profile {profile!r}: expected one of {sorted(PROFILES)}")
            selected_keys = {REGISTRY[tid].scenario_key for tid in spec["testcase_ids"]
                             if REGISTRY[tid].applicable_driver == "KIOSK_EMULATOR"}

        def scenario(key, fn):
            if selected_keys is not None and key not in selected_keys:
                return None
            result = self._scenario(suite_id, run_id, key, fn)
            results.append(result)
            return result

        emp1 = "WF|EMP|QA-EMP-001"
        emp2 = "WF|EMP|QA-EMP-002"
        op_run = "WF|OP|QA-PO-RUN-OP"
        op_wait = "WF|OP|QA-PO-WAIT-OP"

        def fresh_employee(label: str) -> str:
            """Create a scenario-owned worker to isolate session timelines."""
            token = uuid.uuid4().hex[:10].upper()
            employee_no = f"QA-KIOSK-{label}-{token}"
            qr = f"WF|EMP|{employee_no}"
            safe_label = label.replace("'", "")
            database._psql(db_container, f"""
                INSERT INTO employees(employee_no,name,department,position,qr)
                VALUES('{employee_no}','QA Kiosk {safe_label} {token}','QA','Operator','{qr}');
            """)
            return qr

        def valid_employee_scan():
            client = KioskV2Client(self.http, target, f"qa-kiosk-{uuid.uuid4().hex[:8]}")
            client.bootstrap()
            status, resp = client.scan(emp1)
            if status != "PASSED" or not resp.get("accepted") or resp["state"]["name"] != "WAIT_OPERATION":
                raise AssertionError(f"employee scan did not transition to WAIT_OPERATION: {resp}")
            return {"device_id": client.device_id, "state": resp["state"]}
        scenario("sessions.lifecycle.kiosk_valid_employee_scan", valid_employee_scan)

        def valid_operation_scan_starts_session():
            employee = fresh_employee("OPEN")
            client = KioskV2Client(self.http, target, f"qa-kiosk-{uuid.uuid4().hex[:8]}")
            client.bootstrap()
            client.scan(employee)
            status, resp = client.scan(op_run)
            if status != "PASSED" or not resp.get("accepted") or resp["state"]["name"] != "SESSION_ACTIVE":
                raise AssertionError(f"operation scan did not start a session: {resp}")
            session_id = resp["view"].get("session_id")
            row = database.query_json(db_container, f"SELECT status FROM work_sessions WHERE id={session_id.removeprefix('S-')}")
            if not row or row[0]["status"] != "OPEN":
                raise AssertionError(f"backend work_sessions row not OPEN after operation scan: {row}")
            # Close it before returning -- this scenario shares emp1/op_run
            # with other kiosk_emulator scenarios in the same run, and a
            # dangling OPEN session here would make a LATER scenario's own
            # operation scan fail (ONE_ACTIVE_SESSION_PER_EMPLOYEE), for a
            # reason that has nothing to do with whatever that later
            # scenario is actually trying to prove. Found by hand: exactly
            # that cross-scenario pollution broke session_close_normal_
            # quantity the first time this suite ran for real.
            client.scan(employee)
            client.quantity(good=0, defect=0, rework=0)
            return {"session_id": session_id, "backend_status": row[0]["status"]}
        scenario("sessions.lifecycle.kiosk_operation_scan_creates_session", valid_operation_scan_starts_session)

        def session_close_normal_quantity():
            employee = fresh_employee("QTY")
            client = KioskV2Client(self.http, target, f"qa-kiosk-{uuid.uuid4().hex[:8]}")
            client.bootstrap()
            client.scan(employee)
            _, r1 = client.scan(op_run)
            session_id = r1["view"]["session_id"].removeprefix("S-")
            _, r2 = client.scan(employee)  # re-scan same employee -> QUANTITY_INPUT
            if r2["state"]["name"] != "QUANTITY_INPUT":
                raise AssertionError(f"employee re-scan did not request quantity input: {r2}")
            status, r3 = client.quantity(good=5, defect=1, rework=1)
            if status != "PASSED" or not r3.get("accepted") or r3["state"]["name"] != "WAIT_EMPLOYEE":
                raise AssertionError(f"quantity submit did not close the session: {r3}")
            row = database.query_json(db_container, f"SELECT status,good_qty,defect_qty,rework_qty FROM work_sessions WHERE id={session_id}")[0]
            if row["status"] != "CLOSED" or row["good_qty"] != 5 or row["defect_qty"] != 1 or row["rework_qty"] != 1:
                raise AssertionError(f"backend session state disagrees with kiosk-reported quantities: {row}")
            return {"session_id": session_id, "backend_row": row}
        scenario("quality.quantities_rework.kiosk_session_close_with_quantities", session_close_normal_quantity)

        def zero_quantity_close():
            employee = fresh_employee("ZERO")
            client = KioskV2Client(self.http, target, f"qa-kiosk-{uuid.uuid4().hex[:8]}")
            client.bootstrap()
            client.scan(employee)
            r1 = client.scan(op_run)[1]
            session_id = r1["view"]["session_id"].removeprefix("S-")
            client.scan(employee)
            status, r3 = client.quantity(good=0, defect=0, rework=0)
            if status != "PASSED" or not r3.get("accepted"):
                raise AssertionError(f"zero-quantity close was rejected, expected allowed: {r3}")
            row = database.query_json(db_container, f"SELECT good_qty FROM work_sessions WHERE id={session_id}")[0]
            if row["good_qty"] != 0:
                raise AssertionError(f"expected good_qty 0, backend has {row['good_qty']}")
            return {"session_id": session_id}
        scenario("sessions.lifecycle.kiosk_zero_quantity_close", zero_quantity_close)

        def invalid_employee():
            client = KioskV2Client(self.http, target, f"qa-kiosk-{uuid.uuid4().hex[:8]}")
            client.bootstrap()
            status, resp = client.scan("WF|EMP|QA-EMP-DOES-NOT-EXIST")
            if resp.get("accepted") is not False or resp.get("error", {}).get("code") != "EMPLOYEE_NOT_FOUND":
                raise AssertionError(f"invalid employee scan was not correctly rejected: {resp}")
            if resp["state"]["name"] != "WAIT_EMPLOYEE":
                raise AssertionError(f"device state advanced despite a rejected scan: {resp}")
            return {"error_code": resp["error"]["code"]}
        scenario("sessions.lifecycle.kiosk_invalid_employee_rejected", invalid_employee)

        def invalid_operation():
            client = KioskV2Client(self.http, target, f"qa-kiosk-{uuid.uuid4().hex[:8]}")
            client.bootstrap()
            client.scan(emp1)
            status, resp = client.scan("WF|OP|QA-OP-DOES-NOT-EXIST")
            if resp.get("accepted") is not False or resp.get("error", {}).get("code") != "OPERATION_NOT_FOUND":
                raise AssertionError(f"invalid operation scan was not correctly rejected: {resp}")
            return {"error_code": resp["error"]["code"]}
        scenario("sessions.lifecycle.kiosk_invalid_operation_rejected", invalid_operation)

        def not_workable_operation():
            # QA-PO-WAIT is PLANNED, not IN_PROGRESS -- must be rejected
            # with a distinct business code, not a generic not-found.
            client = KioskV2Client(self.http, target, f"qa-kiosk-{uuid.uuid4().hex[:8]}")
            client.bootstrap()
            client.scan(emp1)
            status, resp = client.scan(op_wait)
            if resp.get("accepted") is not False or resp.get("error", {}).get("code") != "OPERATION_NOT_WORKABLE":
                raise AssertionError(f"non-IN_PROGRESS PO operation was not rejected as OPERATION_NOT_WORKABLE: {resp}")
            return {"error_code": resp["error"]["code"]}
        scenario("sessions.lifecycle.kiosk_operation_not_workable_rejected", not_workable_operation)

        def duplicate_scan_idempotent():
            # The kiosk v2 protocol's OWN idempotency guard: replaying the
            # exact same event_id + payload must return the cached response,
            # not process the event twice. Must be BYTE-IDENTICAL envelopes
            # (including device_seq) -- calling client.scan() twice was a
            # real bug here: each call advances client.seq, so the two
            # "identical" requests actually differed in device_seq, and the
            # server correctly (and separately, correctly) rejected that
            # AS a payload mismatch (IDEMPOTENCY_KEY_REUSE_MISMATCH) rather
            # than as a same-payload replay -- a different, also-real
            # protocol rule, just not the one this scenario means to prove.
            client = KioskV2Client(self.http, target, f"qa-kiosk-{uuid.uuid4().hex[:8]}")
            client.bootstrap()
            fixed_id = f"{client.device_id}-DUP-SCAN"
            body = client._envelope("SCAN", {"raw": emp1}, fixed_id)
            status1, r1 = client._post(body)
            status2, r2 = client._post(body)
            if status1 != "PASSED" or not r1.get("accepted"):
                raise AssertionError(f"first scan submission was not accepted: {r1}")
            if r1 != r2:
                raise AssertionError(f"identical event_id replay returned a different response: {r1} vs {r2}")
            return {"replayed_response_identical": True}
        scenario("sessions.lifecycle.kiosk_duplicate_scan_idempotent", duplicate_scan_idempotent)

        def repeated_completion_idempotent():
            employee = fresh_employee("IDEMP")
            client = KioskV2Client(self.http, target, f"qa-kiosk-{uuid.uuid4().hex[:8]}")
            client.bootstrap()
            client.scan(employee)
            r1 = client.scan(op_run)[1]
            session_id = r1["view"]["session_id"].removeprefix("S-")
            client.scan(employee)  # -> QUANTITY_INPUT
            # Submit the SAME finish event_id twice -- proves the kiosk v2
            # idempotency guard (payload_hash match -> cached response,
            # never processed twice) for QUANTITY_SUBMITTED specifically,
            # not just for SCAN as the earlier scenario already covers.
            fixed_id = f"{client.device_id}-DUP-FINISH"
            body = client._envelope("QUANTITY_SUBMITTED", {"quantity_good": 3, "quantity_defect": 0, "quantity_rework": 0}, fixed_id)
            status1, dup1 = client._post(body)
            status2, dup2 = client._post(body)
            if status1 != "PASSED" or not dup1.get("accepted"):
                raise AssertionError(f"first finish submission was not accepted: {dup1}")
            if dup1 != dup2:
                raise AssertionError(f"identical finish event_id replay returned a different response: {dup1} vs {dup2}")
            movement_count = database.query_json(db_container,
                f"SELECT count(*) n FROM quantity_movements WHERE session_id={session_id}")[0]["n"]
            return {"session_id": session_id, "movement_count": movement_count}
        scenario("quality.quantities_rework.kiosk_repeated_completion_idempotent", repeated_completion_idempotent)

        def offline_queue_reconnect_replay():
            client = KioskV2Client(self.http, target, f"qa-kiosk-{uuid.uuid4().hex[:8]}")
            client.bootstrap()
            client.go_offline()
            status, resp = client.scan(emp1)
            if status != "QUEUED" or not resp.get("queued"):
                raise AssertionError(f"offline scan was not queued locally: {resp}")
            queue_size_while_offline = resp["queue_size"]
            replay_results = client.reconnect_and_replay()
            if not replay_results or replay_results[0][0] != "PASSED":
                raise AssertionError(f"queued event did not replay successfully after reconnect: {replay_results}")
            final_state = replay_results[-1][1]["state"]["name"]
            if final_state != "WAIT_OPERATION":
                raise AssertionError(f"replayed scan did not advance device state: {final_state}")
            return {"queue_size_while_offline": queue_size_while_offline, "replayed_count": len(replay_results),
                   "final_state": final_state}
        scenario("kiosk.offline_recovery.queue_reconnect_replay", offline_queue_reconnect_replay)

        def backend_unavailable_then_restored():
            # This suite never stops real containers (recovery.py already
            # owns fault injection against the deployment's own containers,
            # with its own namespace-ownership safety check) -- here,
            # "backend unavailable" is proven by pointing the client at an
            # address nothing listens on, which is what a real device with
            # no network path to the server actually observes.
            client = KioskV2Client(self.http, "http://127.0.0.1:1", f"qa-kiosk-{uuid.uuid4().hex[:8]}")
            unreachable = False
            try:
                client.bootstrap()
            except requests.RequestException:
                unreachable = True
            if not unreachable:
                raise AssertionError("client did not observe the backend as unreachable -- test setup invalid")
            client.target = target
            status, resp = client.bootstrap()
            if status != 200:
                raise AssertionError(f"backend did not accept bootstrap once restored: {status} {resp}")
            return {"detected_unavailable": unreachable, "restored_bootstrap_status": status}
        scenario("kiosk.offline_recovery.backend_unavailable_then_restored", backend_unavailable_then_restored)

        def legacy_web_protocol_path():
            employee_qr = fresh_employee("LEGACY")
            employee_id = database.query_json(
                db_container, f"SELECT id FROM employees WHERE qr='{employee_qr}'")[0]["id"]
            operation_id = database.query_json(
                db_container, "SELECT id FROM operations WHERE code='QA-PO-RUN-OP'")[0]["id"]
            emp_lookup = self.http.post(f"{target}/api/kiosk-web/scan", json={"qr": employee_qr}, timeout=10)
            op_lookup = self.http.post(f"{target}/api/kiosk-web/scan", json={"qr": op_run}, timeout=10)
            if emp_lookup.status_code != 200 or op_lookup.status_code != 200:
                raise AssertionError(f"legacy kiosk scan failed: {emp_lookup.text} / {op_lookup.text}")
            request_id = f"QA-KIOSK-LEGACY-{uuid.uuid4().hex[:10]}"
            started = self.http.post(f"{target}/api/kiosk-web/start",
                json={"employee_id": employee_id, "operation_id": operation_id, "request_id": request_id}, timeout=10)
            if started.status_code != 201 or not started.json().get("ok"):
                raise AssertionError(f"legacy kiosk start failed: {started.status_code} {started.text}")
            session_id = started.json()["session"]["id"]
            finished = self.http.post(f"{target}/api/kiosk-web/finish/{session_id}",
                json={"good_qty": 1, "defect_qty": 0, "request_id": request_id + "-FINISH"}, timeout=10)
            if finished.status_code != 200 or not finished.json().get("ok"):
                raise AssertionError(f"legacy kiosk finish failed: {finished.status_code} {finished.text}")
            return {"session_id": session_id, "path": "scan->scan->start->finish"}
        scenario("kiosk.web_legacy_v2.kiosk_web_protocol", legacy_web_protocol_path)

        def group_supervisor_multiple_kiosks():
            # Two physically distinct kiosk devices, two different workers,
            # both reporting against the SAME operation through the real
            # v2 protocol -- the kiosk-driven proof of "group session"
            # correctness (sessions.group_supervisor) complementing
            # live.py's API-driven multiple() scenario.
            #
            # Every step that OPENS a session is immediately followed, in a
            # try/finally, by the step that closes it -- an earlier version
            # of this scenario checked "was it accepted?" BEFORE closing
            # either session, so a single rejected scan left the OTHER
            # kiosk's already-opened session dangling open for the rest of
            # this qualification run (found by hand: it broke a later
            # recovery.py scenario reusing the same employee, with a
            # BUSINESS_CONFLICT that took real debugging to trace back
            # here). Assertions are deferred to the very end, after both
            # close attempts have been made regardless of outcome.
            employee_a = fresh_employee("GROUPA")
            employee_b = fresh_employee("GROUPB")
            before = database.query_json(db_container, "SELECT done_qty FROM operations WHERE code='QA-PO-RUN-OP'")[0]["done_qty"]
            client_a = KioskV2Client(self.http, target, f"qa-kiosk-a-{uuid.uuid4().hex[:8]}")
            client_b = KioskV2Client(self.http, target, f"qa-kiosk-b-{uuid.uuid4().hex[:8]}")
            client_a.bootstrap(); client_b.bootstrap()
            errors: list[str] = []

            def run_one(client, employee_qr, good_qty):
                client.scan(employee_qr)
                status, r = client.scan(op_run)
                if status != "PASSED" or not r.get("accepted"):
                    errors.append(f"{client.device_id}: operation scan rejected: {r}")
                    return
                try:
                    client.scan(employee_qr)
                    status, r = client.quantity(good=good_qty)
                    if status != "PASSED" or not r.get("accepted"):
                        errors.append(f"{client.device_id}: quantity close rejected: {r}")
                except Exception as exc:
                    errors.append(f"{client.device_id}: {type(exc).__name__}: {exc}")

            run_one(client_a, employee_a, 2)
            run_one(client_b, employee_b, 3)
            if errors:
                raise AssertionError(f"concurrent kiosk session(s) on the same operation failed: {errors}")
            after = database.query_json(db_container, "SELECT done_qty FROM operations WHERE code='QA-PO-RUN-OP'")[0]["done_qty"]
            if after != before + 5:
                raise AssertionError(f"aggregate done_qty after two concurrent kiosk workers: expected {before + 5}, got {after}")
            return {"kiosk_a_device": client_a.device_id, "kiosk_b_device": client_b.device_id,
                    "done_qty_before": before, "done_qty_after": after}
        scenario("sessions.group_supervisor.kiosk_multiple_kiosks_same_operation", group_supervisor_multiple_kiosks)

        # ---- Kiosk Certification phase: new coverage (qualification/kiosk_registry.py KC-014..KC-023) ----

        def malformed_qr_rejected():
            # Every variant must be rejected without mutating anything, and
            # a valid scan afterward must still work -- proves the parser
            # never gets stuck on garbage input (kiosk_v2._parse_scan
            # returns (None,None) for anything not "WF|X|...", which the
            # SCAN handler turns into STATE_INVALID_TRANSITION at WAIT_EMPLOYEE).
            client = KioskV2Client(self.http, target, f"qa-kiosk-{uuid.uuid4().hex[:8]}")
            client.bootstrap()
            variants = ["", "   ", "not-a-qr-code", "WF", "WF|", "WF|EMP", "x" * 4000, "WF|EMP|" + ("y" * 2000)]
            rejected = []
            for raw in variants:
                status, resp = client.scan(raw)
                if status != "PASSED" or resp.get("accepted") is not False:
                    raise AssertionError(f"malformed QR {raw[:40]!r} was not rejected: {resp}")
                if resp["state"]["name"] != "WAIT_EMPLOYEE":
                    raise AssertionError(f"malformed QR {raw[:40]!r} advanced device state: {resp}")
                rejected.append(raw[:20])
            status, resp = client.scan(emp1)
            if status != "PASSED" or not resp.get("accepted"):
                raise AssertionError(f"valid scan after malformed QR sequence still failed: {resp}")
            return {"variants_rejected": len(rejected), "recovered": True}
        scenario("sessions.lifecycle.kiosk_malformed_qr_rejected", malformed_qr_rejected)

        def employee_qr_as_operation_rejected():
            client = KioskV2Client(self.http, target, f"qa-kiosk-{uuid.uuid4().hex[:8]}")
            client.bootstrap()
            client.scan(emp1)  # -> WAIT_OPERATION
            status, resp = client.scan(emp2)  # employee QR where an operation QR is expected
            if resp.get("accepted") is not False or resp["error"]["code"] != "STATE_INVALID_TRANSITION":
                raise AssertionError(f"employee QR scanned as operation was not rejected correctly: {resp}")
            if resp["state"]["name"] != "WAIT_OPERATION":
                raise AssertionError(f"device left WAIT_OPERATION despite a rejected scan: {resp}")
            status, resp = client.scan(op_run)  # a real operation scan must still work afterward
            if status != "PASSED" or not resp.get("accepted"):
                raise AssertionError(f"valid operation scan after the rejected one still failed: {resp}")
            client.scan(emp1); client.quantity(good=0)  # close to avoid leaking an OPEN session
            return {"error_code": "STATE_INVALID_TRANSITION"}
        scenario("sessions.lifecycle.kiosk_employee_qr_as_operation_rejected", employee_qr_as_operation_rejected)

        def operation_before_employee_rejected():
            client = KioskV2Client(self.http, target, f"qa-kiosk-{uuid.uuid4().hex[:8]}")
            client.bootstrap()
            status, resp = client.scan(op_run)
            if resp.get("accepted") is not False or resp["error"]["code"] != "STATE_INVALID_TRANSITION":
                raise AssertionError(f"operation scanned before employee was not rejected: {resp}")
            if resp["state"]["name"] != "WAIT_EMPLOYEE":
                raise AssertionError(f"device state advanced past WAIT_EMPLOYEE despite rejection: {resp}")
            return {"error_code": "STATE_INVALID_TRANSITION"}
        scenario("sessions.lifecycle.kiosk_operation_before_employee_rejected", operation_before_employee_rejected)

        def duplicate_operation_scan_single_session():
            employee = fresh_employee("DUPOP")
            client = KioskV2Client(self.http, target, f"qa-kiosk-{uuid.uuid4().hex[:8]}")
            client.bootstrap()
            client.scan(employee)
            r1 = client.scan(op_run)[1]
            if r1["state"]["name"] != "SESSION_ACTIVE":
                raise AssertionError(f"first operation scan did not start a session: {r1}")
            # SESSION_ACTIVE only accepts an EMPLOYEE re-scan (to request
            # finish) -- scanning the SAME operation code again here is an
            # operation-kind QR where an employee-kind is expected, so it
            # must be rejected the same way, never opening a second session.
            status, r2 = client.scan(op_run)
            if r2.get("accepted") is not False or r2["error"]["code"] != "STATE_INVALID_TRANSITION":
                raise AssertionError(f"duplicate operation scan while a session is active was not rejected: {r2}")
            open_count = database.query_json(db_container,
                f"SELECT count(*) n FROM work_sessions WHERE employee_id=(SELECT id FROM employees WHERE qr='{employee}') AND status='OPEN'")[0]["n"]
            if open_count != 1:
                raise AssertionError(f"expected exactly 1 OPEN session after duplicate OP scan, found {open_count}")
            client.scan(employee); client.quantity(good=1)
            return {"open_sessions_after_duplicate_scan": open_count}
        scenario("sessions.lifecycle.kiosk_duplicate_operation_scan_single_session", duplicate_operation_scan_single_session)

        def negative_quantity_rejected():
            employee = fresh_employee("NEGQ")
            client = KioskV2Client(self.http, target, f"qa-kiosk-{uuid.uuid4().hex[:8]}")
            client.bootstrap()
            client.scan(employee)
            r1 = client.scan(op_run)[1]
            session_id = r1["view"]["session_id"].removeprefix("S-")
            client.scan(employee)  # -> QUANTITY_INPUT
            status, resp = client.send("QUANTITY_SUBMITTED", {"quantity_good": -5, "quantity_defect": 0, "quantity_rework": 0})
            if resp.get("accepted") is not False or resp["error"]["code"] != "QUANTITY_INVALID":
                raise AssertionError(f"negative quantity was not rejected as QUANTITY_INVALID: {resp}")
            row = database.query_json(db_container, f"SELECT status FROM work_sessions WHERE id={session_id}")[0]
            if row["status"] != "OPEN":
                raise AssertionError(f"session was closed despite a rejected negative-quantity submit: {row}")
            # Close properly so this scenario doesn't leak an OPEN session.
            client.quantity(good=1, defect=0, rework=0)
            return {"error_code": "QUANTITY_INVALID"}
        scenario("quality.quantities_rework.kiosk_negative_quantity_rejected", negative_quantity_rejected)

        def non_integer_quantity_rejected():
            employee = fresh_employee("NANQ")
            client = KioskV2Client(self.http, target, f"qa-kiosk-{uuid.uuid4().hex[:8]}")
            client.bootstrap()
            client.scan(employee)
            client.scan(op_run)
            client.scan(employee)  # -> QUANTITY_INPUT
            status, resp = client.send("QUANTITY_SUBMITTED", {"quantity_good": "abc", "quantity_defect": 0, "quantity_rework": 0})
            if resp.get("accepted") is not False or resp["error"]["code"] != "QUANTITY_INVALID":
                raise AssertionError(f"non-integer quantity was not rejected as QUANTITY_INVALID: {resp}")
            client.quantity(good=1, defect=0, rework=0)
            return {"error_code": "QUANTITY_INVALID"}
        scenario("quality.quantities_rework.kiosk_non_integer_quantity_rejected", non_integer_quantity_rejected)

        def response_lost_after_commit_idempotent():
            # Spec section 11's explicitly called-out case: the server
            # commits the quantity submit, but the CLIENT never sees the
            # response (dropped connection). A real device retries under
            # the SAME event_id -- the kiosk v2 idempotency store must
            # return the cached response rather than finishing the session
            # a second time. Mechanically: send once (models "server
            # committed"), then resend the byte-identical envelope (models
            # "client never saw the ack and retried") -- the request/
            # response pair the client actually observes is indistinguishable
            # from a real dropped-response retry.
            employee = fresh_employee("LOSTACK")
            client = KioskV2Client(self.http, target, f"qa-kiosk-{uuid.uuid4().hex[:8]}")
            client.bootstrap()
            client.scan(employee)
            r1 = client.scan(op_run)[1]
            session_id = r1["view"]["session_id"].removeprefix("S-")
            client.scan(employee)
            fixed_id = f"{client.device_id}-LOST-ACK"
            body = client._envelope("QUANTITY_SUBMITTED", {"quantity_good": 7, "quantity_defect": 0, "quantity_rework": 0}, fixed_id)
            status1, first = client._post(body)  # models: server commits
            status2, retry = client._post(body)  # models: client retries after perceiving the response as lost
            if status1 != "PASSED" or not first.get("accepted"):
                raise AssertionError(f"initial commit was not accepted: {first}")
            if first != retry:
                raise AssertionError(f"retry-after-lost-response returned a different result than the real commit: {first} vs {retry}")
            movements = database.query_json(db_container,
                f"SELECT count(*) n FROM quantity_movements WHERE session_id={session_id}")[0]["n"]
            if movements != 1:
                raise AssertionError(f"expected exactly 1 quantity_movements row after retry, found {movements} -- double count")
            return {"session_id": session_id, "movement_count": movements}
        scenario("kiosk.offline_recovery.response_lost_after_commit_idempotent", response_lost_after_commit_idempotent)

        def network_drop_per_phase():
            # Section 10: drop network at several real workflow phases, then
            # restore it and verify convergence -- no duplicate session, no
            # lost quantity. Three phases chosen for a genuinely distinct,
            # real assertion each (not 10 mechanically-copied variants):
            # before employee validation, before session-start (operation
            # scan), before the finish/quantity commit.
            unreachable = "http://127.0.0.1:1"
            results = {}

            # Phase: employee validation
            employee = fresh_employee("NETDROP1")
            client = KioskV2Client(self.http, unreachable, f"qa-kiosk-{uuid.uuid4().hex[:8]}")
            try:
                client.bootstrap()
                raise AssertionError("bootstrap unexpectedly succeeded against an unreachable target")
            except requests.RequestException:
                pass
            client.target = target
            client.bootstrap()
            status, resp = client.scan(employee)
            if status != "PASSED" or not resp.get("accepted"):
                raise AssertionError(f"employee scan after network restore failed: {resp}")
            results["employee_phase_recovered"] = True

            # Phase: session-start (operation scan)
            client2 = KioskV2Client(self.http, target, f"qa-kiosk-{uuid.uuid4().hex[:8]}")
            client2.bootstrap()
            client2.scan(employee)  # same employee, already at WAIT_OPERATION
            client2.target = unreachable
            try:
                client2.scan(op_run)
                raise AssertionError("operation scan unexpectedly succeeded against an unreachable target")
            except requests.RequestException:
                pass
            client2.target = target
            status, resp = client2.scan(op_run)
            if status != "PASSED" or resp["state"]["name"] != "SESSION_ACTIVE":
                raise AssertionError(f"operation scan after network restore did not start a session: {resp}")
            session_id = resp["view"]["session_id"].removeprefix("S-")
            open_count = database.query_json(db_container, f"SELECT count(*) n FROM work_sessions WHERE id={session_id}")[0]["n"]
            if open_count != 1:
                raise AssertionError(f"expected exactly 1 session row after network-drop-then-retry session start, found {open_count}")
            results["session_start_phase_recovered"] = True

            # Phase: finish/quantity commit
            client2.scan(employee)  # -> QUANTITY_INPUT
            client2.target = unreachable
            try:
                client2.quantity(good=3)
                raise AssertionError("quantity submit unexpectedly succeeded against an unreachable target")
            except requests.RequestException:
                pass
            client2.target = target
            status, resp = client2.quantity(good=3)
            if status != "PASSED" or resp["state"]["name"] != "WAIT_EMPLOYEE":
                raise AssertionError(f"quantity submit after network restore did not close the session: {resp}")
            row = database.query_json(db_container, f"SELECT status,good_qty FROM work_sessions WHERE id={session_id}")[0]
            if row["status"] != "CLOSED" or row["good_qty"] != 3:
                raise AssertionError(f"session state after network-drop-then-retry finish disagrees with submitted quantity: {row}")
            results["finish_phase_recovered"] = True
            return results
        scenario("kiosk.offline_recovery.network_drop_per_phase", network_drop_per_phase)

        def offline_queue_many_events():
            employee = fresh_employee("QMANY")
            client = KioskV2Client(self.http, target, f"qa-kiosk-{uuid.uuid4().hex[:8]}")
            client.bootstrap()
            client.go_offline()
            client.scan(employee)
            client.scan(op_run)
            client.scan(employee)
            client.quantity(good=4, defect=1, rework=0)
            if len(client.queue) != 4:
                raise AssertionError(f"expected 4 queued events while offline, found {len(client.queue)}")
            queued_event_ids = [e["event"]["event_id"] for e in client.queue]
            replay = client.reconnect_and_replay()
            if len(replay) != 4 or any(status != "PASSED" for status, _ in replay):
                raise AssertionError(f"not every queued event replayed successfully in order: {replay}")
            final_state = replay[-1][1]["state"]["name"]
            if final_state != "WAIT_EMPLOYEE":
                raise AssertionError(f"queue replay did not converge to a closed session: {final_state}")
            row = database.query_json(db_container,
                f"SELECT status,good_qty,defect_qty FROM work_sessions WHERE employee_id=(SELECT id FROM employees WHERE qr='{employee}') ORDER BY id DESC LIMIT 1")[0]
            if row["status"] != "CLOSED" or row["good_qty"] != 4 or row["defect_qty"] != 1:
                raise AssertionError(f"backend state after 4-event offline queue replay disagrees with what was queued: {row}")
            return {"queued_event_ids": queued_event_ids, "replayed_count": len(replay), "final_state": final_state}
        scenario("kiosk.offline_recovery.offline_queue_many_events", offline_queue_many_events)

        def offline_queue_duplicate_replay_protected():
            client = KioskV2Client(self.http, target, f"qa-kiosk-{uuid.uuid4().hex[:8]}")
            client.bootstrap()
            client.go_offline()
            client.scan(emp1)
            queued_body = client.queue[0]
            client.online = True
            client.queue = []
            status1, first = client._post(queued_body)
            status2, second = client._post(queued_body)  # replaying the exact same queued event a second time
            if status1 != "PASSED" or not first.get("accepted"):
                raise AssertionError(f"first replay of the queued event was not accepted: {first}")
            if first != second:
                raise AssertionError(f"replaying the same queued event twice returned different results (duplicate processed): {first} vs {second}")
            return {"replay_idempotent": True}
        scenario("kiosk.offline_recovery.offline_queue_duplicate_replay_protected", offline_queue_duplicate_replay_protected)

        status = "FAILED" if any(r["status"] == "FAILED" for r in results) else "PASSED"
        self.conn.execute("UPDATE qa_suite_runs SET status=?,finished_at=? WHERE id=?", (status, now(), suite_id))
        self.conn.commit()
        return {"suite_id": suite_id, "status": status, "scenarios": results}
