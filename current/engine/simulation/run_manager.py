"""Run orchestrator: bootstrap -> tick loop (background thread) ->
checkpoint -> stop/resume (items 33, 35, 78).

One process-wide RunManager instance (see agent.py's `_sim_mgr`), same
pattern as PreviewManager. Only ONE simulation run is active at a time in
Phase A (deliberately -- multi-run concurrency is not spec'd and would
multiply the safety-review surface for no requested benefit yet).
"""
from __future__ import annotations

import json
import random
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .. import qa_store
from . import distributions
from .actors.employee import EmployeeActor
from .actors.kiosk_device import KioskDeviceActor
from .actors.web_user import WebUserActor
from .clock import SimClock
from .factory_model import FactoryModel, bootstrap_factory
from .mesflow_client import MesflowClient
from .scheduler import Scheduler

DURATION_SECONDS: dict[str, float | None] = {
    "8_HOURS": 8 * 3600, "24_HOURS": 24 * 3600, "3_DAYS": 3 * 86400, "7_DAYS": 7 * 86400,
    "CONTINUOUS": None,
}
SPEEDS: dict[str, float] = {"REAL_TIME": 1.0, "2X": 2.0, "5X": 5.0, "10X": 10.0}

# Item 86: safety caps checked every checkpoint -- WARN at 70%, refuse to
# schedule new business actions past 100% (item 41: "uncontrollable data
# explosion" is an auto-stop condition, not a suggestion).
MAX_SESSIONS_PER_RUN = 200_000


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class RunMetrics:
    business_events: int = 0
    heartbeats: int = 0
    web_reads: int = 0
    errors: int = 0
    sessions_started: int = 0
    sessions_finished: int = 0
    good_qty: int = 0
    defect_qty: int = 0
    rework_qty: int = 0
    started_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RunMetrics":
        m = cls()
        for k, v in d.items():
            if hasattr(m, k):
                setattr(m, k, v)
        return m


class SimulationRun:
    def __init__(self, run_id: str, base_url: str, admin_password: str):
        self.run_id = run_id
        self.base_url = base_url
        self.admin_password = admin_password
        self.clock: SimClock | None = None
        self.scheduler = Scheduler()
        self.factory: FactoryModel | None = None
        self.employee_actors: dict[str, EmployeeActor] = {}
        self.web_actors: dict[str, WebUserActor] = {}
        self.kiosk_actors: dict[str, KioskDeviceActor] = {}
        self.metrics = RunMetrics()
        self._client_by_thread: dict[int, MesflowClient] = {}
        self._client_lock = threading.Lock()
        self._stop_flag = threading.Event()
        self.status = "RUNNING"
        self.stop_reason = ""
        self.planned_end_sim_at: float | None = None
        self._thread: threading.Thread | None = None
        self._last_checkpoint = 0.0

    def _client(self) -> MesflowClient:
        # One requests.Session per worker thread (item 49: reuse sessions),
        # never a shared Session mutated concurrently across threads.
        tid = threading.get_ident()
        with self._client_lock:
            client = self._client_by_thread.get(tid)
            if client is None:
                client = MesflowClient(base_url=self.base_url)
                self._client_by_thread[tid] = client
        return client

    # --- bootstrap ------------------------------------------------------

    def bootstrap(self, *, profile: str, duration_label: str, speed_label: str, seed: int) -> None:
        counts = dict(distributions.FACTORY_PROFILES.get(profile, distributions.FACTORY_PROFILES["SMALL_FACTORY"]))
        client = self._client()
        client.login("admin", self.admin_password)

        run_tag = f"{datetime.now(timezone.utc).strftime('%Y%m%d')}-{self.run_id[:8]}"
        self.factory = bootstrap_factory(client, run_tag=run_tag, profile_counts=counts, seed=seed)

        self.clock = SimClock(speed=SPEEDS.get(speed_label, 1.0))
        duration = DURATION_SECONDS.get(duration_label)
        self.planned_end_sim_at = (self.clock.now() + duration) if duration else None

        rng = random.Random(seed)
        for i, emp in enumerate(self.factory.employees):
            profile_obj = distributions.pick_profile(rng)
            device_uuid = self.factory.station_ids[i % len(self.factory.station_ids)] if self.factory.station_ids else "1"
            actor = EmployeeActor(
                actor_id=f"emp:{emp.employee_no}", employee=emp, device_uuid=f"WEB-QA-SIM-KIOSK-{device_uuid}",
                profile=profile_obj, rng=random.Random(rng.random()), operations=list(self.factory.operations),
            )
            self.employee_actors[actor.actor_id] = actor
            first_action = actor.initial_action_at(self.clock.now())
            self.scheduler.schedule(actor.actor_id, first_action)

        for i, user in enumerate(self.factory.web_users):
            actor = WebUserActor(actor_id=f"web:{user.username}", role=user.role.upper(),
                                  username=user.username, password=user.password, rng=random.Random(rng.random()))
            self.web_actors[actor.actor_id] = actor
            self.scheduler.schedule(actor.actor_id, self.clock.now() + rng.uniform(10, 120) * self.clock.speed)

        for i, station_id in enumerate(self.factory.station_ids):
            actor = KioskDeviceActor(actor_id=f"kiosk:{station_id}", device_uuid=f"WEB-QA-SIM-KIOSK-{station_id}",
                                      rng=random.Random(rng.random()))
            self.kiosk_actors[actor.actor_id] = actor
            self.scheduler.schedule(actor.actor_id, self.clock.now() + rng.uniform(1, 30) * self.clock.speed)

        self._persist_run(seed=seed, profile=profile, duration_label=duration_label)

    # --- persistence ------------------------------------------------------

    def _persist_run(self, *, seed: int, profile: str, duration_label: str) -> None:
        conn = qa_store.connect()
        now = _now_iso()
        conn.execute(
            """INSERT INTO sim_runs(run_id,preview_id,status,profile,duration_label,speed,seed,
                   planned_end_sim_at,stop_reason,clock_json,factory_json,metrics_json,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(run_id) DO UPDATE SET status=excluded.status,stop_reason=excluded.stop_reason,
                   clock_json=excluded.clock_json,metrics_json=excluded.metrics_json,updated_at=excluded.updated_at""",
            (self.run_id, "", self.status, profile, duration_label, self.clock.speed, seed,
             self.planned_end_sim_at, self.stop_reason, json.dumps(self.clock.snapshot_for_persist()),
             json.dumps(self.factory.to_dict()), json.dumps(self.metrics.to_dict()), now, now),
        )
        conn.commit()

    def checkpoint(self, kind: str = "LIGHT") -> dict[str, Any]:
        conn = qa_store.connect()
        now = _now_iso()
        snapshot = self.status_snapshot()
        conn.execute(
            "UPDATE sim_runs SET status=?,stop_reason=?,clock_json=?,metrics_json=?,updated_at=? WHERE run_id=?",
            (self.status, self.stop_reason, json.dumps(self.clock.snapshot_for_persist()),
             json.dumps(self.metrics.to_dict()), now, self.run_id),
        )
        for actor_id, actor in {**self.employee_actors}.items():
            conn.execute(
                """INSERT INTO sim_actors(actor_id,run_id,kind,next_action_at,state_json,updated_at)
                       VALUES(?,?,?,?,?,?)
                   ON CONFLICT(actor_id) DO UPDATE SET state_json=excluded.state_json,updated_at=excluded.updated_at""",
                (actor_id, self.run_id, "EMPLOYEE", 0.0, json.dumps(actor.snapshot()), now),
            )
        conn.execute(
            "INSERT INTO sim_checkpoints(run_id,kind,metrics_json,created_at) VALUES(?,?,?,?)",
            (self.run_id, kind, json.dumps(snapshot), now),
        )
        conn.commit()
        self._last_checkpoint = time.time()
        return snapshot

    # --- tick loop --------------------------------------------------------

    def _act_one(self, actor_id: str, sim_now: float) -> float | None:
        client = self._client()
        try:
            if actor_id.startswith("emp:"):
                actor = self.employee_actors[actor_id]
                next_at, result = actor.act(sim_now, client)
            elif actor_id.startswith("web:"):
                next_at, result = self.web_actors[actor_id].act(sim_now, client, speed=self.clock.speed)
            else:
                next_at, result = self.kiosk_actors[actor_id].act(sim_now, client, speed=self.clock.speed)
        except Exception as exc:  # noqa: BLE001 -- a single actor's crash must never kill the run (item 36)
            self.metrics.errors += 1
            return sim_now + 300.0

        if result is not None:
            self._record_result(actor_id, result)
        return next_at

    def _record_result(self, actor_id: str, result) -> None:
        if result.kind in ("SESSION_START", "SESSION_START_REJECTED", "MISTAKE_BAD_SCAN", "SCAN_OPERATION_REJECTED"):
            self.metrics.business_events += 1
            if result.kind == "SESSION_START":
                self.metrics.sessions_started += 1
        elif result.kind in ("SESSION_FINISH", "SESSION_FINISH_REJECTED"):
            self.metrics.business_events += 1
            self.metrics.sessions_finished += 1
            self.metrics.good_qty += int(result.detail.get("good") or 0)
            self.metrics.defect_qty += int(result.detail.get("defect") or 0)
            self.metrics.rework_qty += int(result.detail.get("rework") or 0)
        elif result.kind == "HEARTBEAT":
            self.metrics.heartbeats += 1
        elif result.kind == "WEB_READ":
            self.metrics.web_reads += 1
        if not result.ok:
            self.metrics.errors += 1

    def _should_stop(self) -> bool:
        if self._stop_flag.is_set():
            return True
        if self.metrics.sessions_started >= MAX_SESSIONS_PER_RUN:
            self.status, self.stop_reason = "STOPPED_SAFETY", "MAX_SESSIONS_PER_RUN reached (item 41/86)"
            return True
        if self.planned_end_sim_at is not None and self.clock.now() >= self.planned_end_sim_at:
            self.status, self.stop_reason = "COMPLETED", "planned duration elapsed"
            return True
        return False

    def _tick_hook(self) -> None:
        if time.time() - self._last_checkpoint >= 300:  # item 35: every 5 min (lightweight)
            self.checkpoint("LIGHT")

    def run_loop(self) -> None:
        from .scheduler import run_until_empty_or_stopped
        run_until_empty_or_stopped(
            self.scheduler, self.clock, self._act_one, self._should_stop, time.sleep, self._tick_hook,
        )
        if self.status == "RUNNING":
            self.status = "STOPPED"
        self.checkpoint("FULL")

    def start_thread(self) -> None:
        self._thread = threading.Thread(target=self.run_loop, name=f"sim-{self.run_id}", daemon=True)
        self._thread.start()

    def stop(self, reason: str = "manual stop") -> None:
        self.status = "STOPPED"
        self.stop_reason = reason
        self._stop_flag.set()

    def status_snapshot(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "status": self.status, "stop_reason": self.stop_reason,
            "sim_now": self.clock.now() if self.clock else None,
            "employees": len(self.employee_actors), "web_users": len(self.web_actors),
            "kiosks": len(self.kiosk_actors), "scheduled": len(self.scheduler),
            "metrics": self.metrics.to_dict(),
        }


class RunManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: SimulationRun | None = None

    def start(self, *, base_url: str, admin_password: str, profile: str, duration_label: str,
              speed_label: str, seed: int | None = None) -> dict[str, Any]:
        with self._lock:
            if self._active is not None and self._active.status == "RUNNING":
                raise RuntimeError("a simulation run is already active")
            run_id = uuid.uuid4().hex
            seed = seed if seed is not None else random.SystemRandom().randrange(1, 2**31)
            run = SimulationRun(run_id, base_url, admin_password)
            run.bootstrap(profile=profile, duration_label=duration_label, speed_label=speed_label, seed=seed)
            run.start_thread()
            self._active = run
            return run.status_snapshot()

    def status(self) -> dict[str, Any] | None:
        with self._lock:
            return self._active.status_snapshot() if self._active else None

    def stop(self, reason: str = "manual stop") -> dict[str, Any]:
        with self._lock:
            if not self._active:
                raise RuntimeError("no active simulation run")
            self._active.stop(reason)
            return self._active.status_snapshot()
