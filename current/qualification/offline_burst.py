"""OFFLINE_BURST (spec section 3): many kiosks disconnect, queue events
locally (including deliberate duplicate/retry resends), then reconnect and
replay simultaneously -- proving the real kiosk v2 offline queue mechanism
(kiosk_emulator.py's KioskV2Client, reused here, not reimplemented)
converges correctly under real concurrency, not just for one device at a
time the way kiosk_emulator.py's own offline_queue_reconnect_replay
scenario already does.

Each logical kiosk gets a real, scenario-owned QA employee in the isolated
sandbox. A burst of N kiosks therefore proves N real business actors
converge, rather than hiding N-2 clients behind rejected filler events.
"""
from __future__ import annotations

import random
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import requests

from .database import DeterministicDatabase
from .evidence import EvidenceStore
from .integrity_runner import IntegrityRunner
from .kiosk_emulator import KioskV2Client
from .resource_sampler import ResourceSampler
from .scenario_runner import ScenarioRunner
from .store import connect, now

PROFILES = {
    "CI": {"kiosk_count": 5},
    "QUALIFICATION": {"kiosk_count": 20},
}


class OfflineBurstRunner:
    def __init__(self, evidence_root: Path):
        self.conn = connect()
        self.evidence = EvidenceStore(evidence_root)
        self.evidence_root = evidence_root
        self._integrity = IntegrityRunner(evidence_root)
        self._sampler = ResourceSampler(evidence_root)
        self._runner = ScenarioRunner(evidence_root, scenario_version="offline-burst-v1",
                                      driver="KIOSK_EMULATOR", evidence_kind="OFFLINE_BURST_EVIDENCE")

    def run(self, run_id: str, deployment: dict[str, Any], *, profile: str = "CI",
            kiosk_count: int | None = None, seed: int | None = None) -> dict[str, Any]:
        spec = PROFILES.get(profile.upper())
        if not spec:
            raise ValueError(f"unknown offline_burst profile: {profile!r} (expected one of {sorted(PROFILES)})")
        n = kiosk_count if kiosk_count is not None else spec["kiosk_count"]
        if n < 1:
            raise ValueError("kiosk_count must be at least 1")
        worker_count = n
        seed = seed if seed is not None else random.SystemRandom().randrange(1, 2**31)

        target = deployment["target_url"].rstrip("/")
        db_container = deployment["db_container"]
        database = DeterministicDatabase(self.evidence_root)
        http = requests.Session()

        suite_id = f"suite-{uuid.uuid4().hex}"
        self.conn.execute("""INSERT INTO qa_suite_runs(id,qualification_run_id,suite_key,layer,required,status,
          started_at,command_json) VALUES(?,?,'offline_burst','recovery',1,'RUNNING',?,'[]')""", (suite_id, run_id, now()))
        self.conn.commit()
        results: list[dict[str, Any]] = []

        def scenario(key, fn):
            result = self._runner.run(suite_id, run_id, key, fn)
            results.append(result)
            return result

        # Each worker gets its OWN disposable employee AND its OWN
        # disposable operation (cloned from QA-PO-RUN-OP's production
        # order/part, IN_PROGRESS, no predecessor) -- found by hand the
        # first time this ran for real: N kiosks all targeting the SAME
        # shared QA-PO-RUN-OP hit a genuine, unrelated MESFlow business
        # rule (station/operation session-time-overlap capacity), rejecting
        # most of them with BUSINESS_CONFLICT. That capacity limit is real
        # and correct -- it has nothing to do with the offline queue/
        # reconnect/duplicate-handling mechanism this suite exists to
        # prove, so each worker gets independent resources instead of
        # sharing one contended operation.
        worker_employees: list[str] = []
        worker_operations: list[dict[str, Any]] = []
        base = database.query_json(db_container,
            "SELECT part_id,production_order_id FROM operations WHERE code='QA-PO-RUN-OP'")[0]
        for i in range(worker_count):
            employee_no = f"QA-BURST-{seed}-{i:04d}"
            employee_qr = f"WF|EMP|{employee_no}"
            database._psql(db_container, f"""
                INSERT INTO employees(employee_no,name,department,position,qr)
                VALUES('{employee_no}','QA Burst Worker {i:04d}','QA','Operator','{employee_qr}');
            """)
            worker_employees.append(employee_qr)
            op_code = f"QA-BURST-OP-{seed}-{i:04d}"
            op_qr_value = f"WF|OP|{op_code}"
            database._psql(db_container, f"""
                INSERT INTO operations(production_order_id,part_id,code,name,done_qty,defect_qty,rework_qty,status,sort_order,qr)
                VALUES({base['production_order_id']},{base['part_id']},'{op_code}','QA Burst Operation {i:04d}',0,0,0,'IN_PROGRESS',10+{i},'{op_qr_value}');
            """)
            worker_operations.append({"code": op_code, "qr": op_qr_value})

        kiosks: list[KioskV2Client] = []
        kiosk_roles: dict[str, str] = {}

        def online_phase():
            # "operate online": every kiosk does at least one real,
            # accepted round trip before anything goes offline.
            for i in range(n):
                client = KioskV2Client(http, target, f"qa-burst-{seed}-{i:03d}-{uuid.uuid4().hex[:6]}")
                status, body = client.bootstrap()
                if status != 200 or not body.get("ok", True):
                    raise AssertionError(f"kiosk {client.device_id} failed to bootstrap online: {status} {body}")
                kiosks.append(client)
                kiosk_roles[client.device_id] = "worker"
            return {"kiosk_count": len(kiosks), "worker_count": worker_count}
        scenario("kiosk.offline_recovery.burst_online_phase", online_phase)

        queued_event_count = 0
        duplicate_event_count = 0

        def queue_phase():
            nonlocal queued_event_count, duplicate_event_count
            for client in kiosks:
                client.go_offline()
            for i, employee_qr in enumerate(worker_employees):
                client = kiosks[i]
                client.scan(employee_qr)
                client.scan(worker_operations[i]["qr"])
                client.scan(employee_qr)  # re-scan -> QUANTITY_INPUT (required before a quantity submit is accepted)
                queued_event_count += 3
                request_qty_id = f"{client.device_id}-QTY"
                body = client._envelope("QUANTITY_SUBMITTED", {"quantity_good": 1, "quantity_defect": 0, "quantity_rework": 0}, request_qty_id)
                client.queue.append(body)
                queued_event_count += 1
                # Deliberate retry: the SAME event queued a second time,
                # simulating a client that re-queues after a local timeout
                # it never actually confirmed -- server-side idempotency
                # (payload_hash-keyed cache) must make this a no-op replay,
                # not a second quantity movement.
                client.queue.append(dict(body))
                duplicate_event_count += 1
            return {"queued_event_count": queued_event_count, "duplicate_event_count": duplicate_event_count,
                    "queue_sizes": {c.device_id: len(c.queue) for c in kiosks}}
        scenario("kiosk.offline_recovery.burst_queue_phase", queue_phase)

        reconnect_results: dict[str, list[tuple[str, dict[str, Any]]]] = {}

        def reconnect_phase():
            started = time.time()
            def replay_one(client: KioskV2Client):
                return client.device_id, client.reconnect_and_replay()
            with ThreadPoolExecutor(max_workers=max(n, 1)) as pool:
                for device_id, replay in pool.map(replay_one, kiosks):
                    reconnect_results[device_id] = replay
            elapsed = time.time() - started
            queues_drained = all(len(c.queue) == 0 for c in kiosks)
            if not queues_drained:
                raise AssertionError("not every kiosk's local queue drained to zero after reconnect_and_replay")
            return {"reconnect_wall_seconds": round(elapsed, 3), "kiosks_reconnected": len(kiosks),
                    "queues_drained_to_zero": queues_drained}
        scenario("kiosk.offline_recovery.burst_reconnect_phase", reconnect_phase)

        def convergence_phase():
            # Every WORKER's events must have replayed successfully
            # (including the deliberately duplicated one, idempotently).
            broken_workers = {}
            for i in range(worker_count):
                device_id = kiosks[i].device_id
                replies = reconnect_results[device_id]
                # HTTP-level "PASSED" alone is not enough -- a business
                # rejection (wrong device state, a dependency gate, ...)
                # still answers 200 with accepted:false; checking only the
                # transport status here was a real bug (see
                # kiosk.offline_recovery.workflow's own docstring for the
                # matching fix in live.py) that let a silently-rejected
                # quantity submit read as a clean replay.
                if any(status != "PASSED" or body.get("accepted") is False for status, body in replies):
                    broken_workers[device_id] = replies
            if broken_workers:
                raise AssertionError(f"worker kiosk(s) had a non-PASSED replayed event: {broken_workers}")

            # Each worker owns its own disposable operation -- done_qty
            # must be EXACTLY 1 on each one (the duplicated event must not
            # double-count), never 0 (the queued event must not be lost)
            # and never >1 (no duplicate quantity movement).
            wrong_done_qty = {}
            for op in worker_operations:
                qty = database.query_json(db_container, f"SELECT done_qty FROM operations WHERE code='{op['code']}'")[0]["done_qty"]
                if qty != 1:
                    wrong_done_qty[op["code"]] = qty
            if wrong_done_qty:
                raise AssertionError(f"per-worker operation done_qty after burst (expected 1 each): {wrong_done_qty}")

            sessions_created = database.query_json(db_container,
                "SELECT count(*) n FROM work_sessions ws JOIN operations o ON o.id=ws.operation_id WHERE o.code LIKE 'QA-BURST-OP-%'")[0]["n"]
            if sessions_created != worker_count:
                raise AssertionError(f"expected exactly {worker_count} sessions (one per worker operation), found {sessions_created} "
                                     f"(a lost event or a duplicate-created second session)")

            integrity = self._integrity.check(run_id, db_container, evidence_name="offline-burst-invariants")
            broken_invariants = {k: v["violations"] for k, v in integrity.items() if v["status"] == "FAILED"}
            if broken_invariants:
                raise AssertionError(f"domain invariants failed after offline burst convergence: {broken_invariants}")

            return {"per_worker_done_qty": 1, "worker_count": worker_count, "sessions_created_by_workers": sessions_created,
                    "integrity": {k: v["status"] for k, v in integrity.items()}}
        scenario("kiosk.offline_recovery.burst_convergence", convergence_phase)

        sample = self._sampler.sample_once(run_id, deployment.get("id", ""), app_container=deployment.get("app_container", ""),
                                           db_container=db_container, target_url=target)

        status = "FAILED" if any(r["status"] == "FAILED" for r in results) else "PASSED"
        summary = {"seed": seed, "profile": profile.upper(), "kiosk_count": n, "worker_count": worker_count,
                  "queued_event_count": queued_event_count, "duplicate_event_count": duplicate_event_count,
                  "resource_sample": sample}
        self.evidence.write_json(run_id, "offline-burst-summary.json", summary, kind="OFFLINE_BURST_EVIDENCE")
        self.conn.execute("UPDATE qa_suite_runs SET status=?,finished_at=?,summary_json=? WHERE id=?",
                          (status, now(), __import__("json").dumps(summary, default=str), suite_id))
        self.conn.commit()
        return {"suite_id": suite_id, "status": status, "scenarios": results, "summary": summary}
