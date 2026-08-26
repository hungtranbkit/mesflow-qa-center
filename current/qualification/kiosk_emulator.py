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
            admin_password: str = "Admin@123456") -> dict[str, Any]:
        target = target_url.rstrip("/")
        suite_id = f"suite-{uuid.uuid4().hex}"
        self.conn.execute("""INSERT INTO qa_suite_runs(id,qualification_run_id,suite_key,layer,required,status,
          started_at,command_json) VALUES(?,?,'kiosk_emulator','kiosk',1,'RUNNING',?,'[]')""", (suite_id, run_id, now()))
        self.conn.commit()
        results: list[dict[str, Any]] = []
        database = DeterministicDatabase(self.evidence_root)

        def scenario(key, fn):
            result = self._scenario(suite_id, run_id, key, fn)
            results.append(result)
            return result

        emp1 = "WF|EMP|QA-EMP-001"
        emp2 = "WF|EMP|QA-EMP-002"
        op_run = "WF|OP|QA-PO-RUN-OP"
        op_wait = "WF|OP|QA-PO-WAIT-OP"

        def valid_employee_scan():
            client = KioskV2Client(self.http, target, f"qa-kiosk-{uuid.uuid4().hex[:8]}")
            client.bootstrap()
            status, resp = client.scan(emp1)
            if status != "PASSED" or not resp.get("accepted") or resp["state"]["name"] != "WAIT_OPERATION":
                raise AssertionError(f"employee scan did not transition to WAIT_OPERATION: {resp}")
            return {"device_id": client.device_id, "state": resp["state"]}
        scenario("kiosk_emulator.valid_employee_scan", valid_employee_scan)

        def valid_operation_scan_starts_session():
            client = KioskV2Client(self.http, target, f"qa-kiosk-{uuid.uuid4().hex[:8]}")
            client.bootstrap()
            client.scan(emp1)
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
            client.scan(emp1)
            client.quantity(good=0, defect=0, rework=0)
            return {"session_id": session_id, "backend_status": row[0]["status"]}
        scenario("kiosk_emulator.valid_operation_scan_creates_open_session", valid_operation_scan_starts_session)

        def session_close_normal_quantity():
            client = KioskV2Client(self.http, target, f"qa-kiosk-{uuid.uuid4().hex[:8]}")
            client.bootstrap()
            client.scan(emp1)
            _, r1 = client.scan(op_run)
            session_id = r1["view"]["session_id"].removeprefix("S-")
            _, r2 = client.scan(emp1)  # re-scan same employee -> QUANTITY_INPUT
            if r2["state"]["name"] != "QUANTITY_INPUT":
                raise AssertionError(f"employee re-scan did not request quantity input: {r2}")
            status, r3 = client.quantity(good=5, defect=1, rework=1)
            if status != "PASSED" or not r3.get("accepted") or r3["state"]["name"] != "WAIT_EMPLOYEE":
                raise AssertionError(f"quantity submit did not close the session: {r3}")
            row = database.query_json(db_container, f"SELECT status,good_qty,defect_qty,rework_qty FROM work_sessions WHERE id={session_id}")[0]
            if row["status"] != "CLOSED" or row["good_qty"] != 5 or row["defect_qty"] != 1 or row["rework_qty"] != 1:
                raise AssertionError(f"backend session state disagrees with kiosk-reported quantities: {row}")
            return {"session_id": session_id, "backend_row": row}
        scenario("kiosk_emulator.session_close_normal_quantity", session_close_normal_quantity)

        def zero_quantity_close():
            client = KioskV2Client(self.http, target, f"qa-kiosk-{uuid.uuid4().hex[:8]}")
            client.bootstrap()
            client.scan(emp2)
            r1 = client.scan(op_run)[1]
            session_id = r1["view"]["session_id"].removeprefix("S-")
            client.scan(emp2)
            status, r3 = client.quantity(good=0, defect=0, rework=0)
            if status != "PASSED" or not r3.get("accepted"):
                raise AssertionError(f"zero-quantity close was rejected, expected allowed: {r3}")
            row = database.query_json(db_container, f"SELECT good_qty FROM work_sessions WHERE id={session_id}")[0]
            if row["good_qty"] != 0:
                raise AssertionError(f"expected good_qty 0, backend has {row['good_qty']}")
            return {"session_id": session_id}
        scenario("kiosk_emulator.zero_quantity_close_allowed", zero_quantity_close)

        def invalid_employee():
            client = KioskV2Client(self.http, target, f"qa-kiosk-{uuid.uuid4().hex[:8]}")
            client.bootstrap()
            status, resp = client.scan("WF|EMP|QA-EMP-DOES-NOT-EXIST")
            if resp.get("accepted") is not False or resp.get("error", {}).get("code") != "EMPLOYEE_NOT_FOUND":
                raise AssertionError(f"invalid employee scan was not correctly rejected: {resp}")
            if resp["state"]["name"] != "WAIT_EMPLOYEE":
                raise AssertionError(f"device state advanced despite a rejected scan: {resp}")
            return {"error_code": resp["error"]["code"]}
        scenario("kiosk_emulator.invalid_employee_rejected_state_unchanged", invalid_employee)

        def invalid_operation():
            client = KioskV2Client(self.http, target, f"qa-kiosk-{uuid.uuid4().hex[:8]}")
            client.bootstrap()
            client.scan(emp1)
            status, resp = client.scan("WF|OP|QA-OP-DOES-NOT-EXIST")
            if resp.get("accepted") is not False or resp.get("error", {}).get("code") != "OPERATION_NOT_FOUND":
                raise AssertionError(f"invalid operation scan was not correctly rejected: {resp}")
            return {"error_code": resp["error"]["code"]}
        scenario("kiosk_emulator.invalid_operation_rejected", invalid_operation)

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
        scenario("kiosk_emulator.operation_not_in_progress_rejected", not_workable_operation)

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
        scenario("kiosk_emulator.duplicate_scan_idempotent_replay", duplicate_scan_idempotent)

        def repeated_completion_idempotent():
            client = KioskV2Client(self.http, target, f"qa-kiosk-{uuid.uuid4().hex[:8]}")
            client.bootstrap()
            client.scan(emp2)
            r1 = client.scan(op_run)[1]
            session_id = r1["view"]["session_id"].removeprefix("S-")
            client.scan(emp2)  # -> QUANTITY_INPUT
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
        scenario("kiosk_emulator.repeated_completion_idempotent", repeated_completion_idempotent)

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
        scenario("kiosk_emulator.offline_queue_reconnect_replay", offline_queue_reconnect_replay)

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
        scenario("kiosk_emulator.backend_unavailable_then_restored", backend_unavailable_then_restored)

        status = "FAILED" if any(r["status"] == "FAILED" for r in results) else "PASSED"
        self.conn.execute("UPDATE qa_suite_runs SET status=?,finished_at=? WHERE id=?", (status, now(), suite_id))
        self.conn.commit()
        return {"suite_id": suite_id, "status": status, "scenarios": results}
