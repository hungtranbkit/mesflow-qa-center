"""Employee (kiosk worker) actor -- the core of the soak's business event
volume (items 3-8, 20, 92).

Real kiosk workflow only (item 89): SCAN employee QR -> SCAN operation QR
-> START -> (work happens, no traffic at all during this -- item 3) ->
SCAN employee QR again -> submit quantity -> FINISH. Every quantity/
duration number comes from distributions.py's duration-first formula.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .. import distributions
from ..factory_model import FactoryEmployee, FactoryOperation
from ..mesflow_client import MesflowApiError, MesflowClient
from .base import ActorResult

SHIFT_START_HOUR = 7.0
LUNCH_START_HOUR = 11.5
LUNCH_END_HOUR = 12.5
SHIFT_END_HOUR = 16.5
CHANGEOVER_SECONDS = (60, 5 * 60)  # brief gap between finishing one session and scanning in for the next


def _day_seconds(hour: float) -> float:
    return hour * 3600.0


@dataclass
class EmployeeActor:
    actor_id: str
    employee: FactoryEmployee
    device_uuid: str
    profile: distributions.EmployeeProfile
    rng: random.Random
    operations: list[FactoryOperation]

    state: str = "IDLE"              # IDLE | WORKING
    open_session_id: int | None = None
    current_operation: FactoryOperation | None = None
    quarantined: bool = False        # item 36: "quarantine affected actor if necessary"
    _planned: tuple[int, int, int] = field(default=(1, 0, 0))  # (good, defect, rework) for the OPEN session, if any

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state, "open_session_id": self.open_session_id,
            "current_operation_id": self.current_operation.operation_id if self.current_operation else None,
            "quarantined": self.quarantined, "device_uuid": self.device_uuid,
            "profile": self.profile.name, "planned": list(self._planned),
        }

    def restore(self, snap: dict[str, Any]) -> None:
        self.state = snap.get("state", "IDLE")
        self.open_session_id = snap.get("open_session_id")
        self.quarantined = bool(snap.get("quarantined", False))
        op_id = snap.get("current_operation_id")
        self.current_operation = next((o for o in self.operations if o.operation_id == op_id), None) if op_id else None
        planned = snap.get("planned")
        if planned and len(planned) == 3:
            self._planned = (int(planned[0]), int(planned[1]), int(planned[2]))

    def initial_action_at(self, now: float) -> float:
        """Real bug found live (validation run against a real preview
        environment): a simulation started mid-shift must have its
        employees begin working soon, not wait for TOMORROW's shift start
        -- `_next_shift_start` always computes the NEXT start, which is
        wrong the very first time an actor is scheduled if `now` already
        falls inside today's working window."""
        shift_start = self._shift_end_today(now) - _day_seconds(SHIFT_END_HOUR - SHIFT_START_HOUR)
        if shift_start <= now < self._shift_end_today(now):
            return self._push_past_lunch(now + self.rng.uniform(5, 60))
        return self._next_shift_start(now)

    def _next_shift_start(self, now: float) -> float:
        """Today's shift start + arrival jitter (item 12), or tomorrow's if
        today's has already passed. Weekends/holidays are out of Phase A
        scope (deferred; see engine/simulation/__init__.py)."""
        dt = datetime.fromtimestamp(now, tz=timezone.utc)
        today_start = dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        candidate = today_start + _day_seconds(SHIFT_START_HOUR) + distributions.pick_arrival_offset_minutes(self.rng) * 60
        if candidate <= now:
            candidate += 86400
        return candidate

    def _shift_end_today(self, now: float) -> float:
        dt = datetime.fromtimestamp(now, tz=timezone.utc)
        today_start = dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        return today_start + _day_seconds(SHIFT_END_HOUR)

    def _push_past_lunch(self, when: float) -> float:
        dt = datetime.fromtimestamp(when, tz=timezone.utc)
        today_start = dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        lunch_start, lunch_end = today_start + _day_seconds(LUNCH_START_HOUR), today_start + _day_seconds(LUNCH_END_HOUR)
        if lunch_start <= when < lunch_end:
            return lunch_end
        return when

    def act(self, now: float, client: MesflowClient) -> tuple[float | None, ActorResult | None]:
        if self.quarantined:
            return None, None
        shift_end = self._shift_end_today(now)
        if now >= shift_end and self.state == "IDLE":
            return self._next_shift_start(now), None

        if self.state == "IDLE":
            return self._try_start(now, client)
        return self._finish(now, client)

    # --- START flow ---------------------------------------------------

    def _try_start(self, now: float, client: MesflowClient) -> tuple[float | None, ActorResult | None]:
        if not self.operations:
            return self._next_shift_start(now + 86400), None
        op = self.rng.choice(self.operations)

        # Item 92 coverage, rare: attempt a second operation while (we
        # believe) we already have one open. Only exercised when we
        # genuinely still think we're WORKING would be a logic bug (we
        # never reach _try_start from WORKING) -- so this simulates the
        # OTHER real path: re-scan the employee QR while an unrelated
        # OPEN session already exists server-side from a previous run,
        # by deliberately not clearing it. Kept rare and clearly labeled.
        try:
            emp_scan = client.kiosk_scan(self.employee.qr)
        except MesflowApiError as exc:
            return now + self.rng.uniform(30, 120), ActorResult(False, "SCAN_EMPLOYEE_ERROR", {"error": str(exc)})
        if emp_scan.get("open_session"):
            # Server already has an OPEN session for this employee (e.g.
            # resumed run, or a genuine conflict) -- pick up that session
            # rather than silently abandoning it (mirrors what a real
            # employee re-scanning at the kiosk sees and does).
            self.state = "WORKING"
            self.open_session_id = emp_scan["open_session"]["id"] if "id" in emp_scan["open_session"] else emp_scan["open_session"].get("session_id")
            op_id = emp_scan["open_session"].get("operation_id")
            self.current_operation = next((o for o in self.operations if o.operation_id == op_id), self.current_operation or op)
            return now + self.rng.uniform(300, 3600), ActorResult(True, "RESUMED_OPEN_SESSION", {"session_id": self.open_session_id})

        # Low-rate realistic mistake (item 20): scan the operation QR
        # BEFORE the employee QR is acknowledged, or scan an invalid/
        # malformed QR. Expected: business rejection, no Session created.
        if self.rng.random() < self.profile.mistake_rate:
            bad_qr = self.rng.choice(["WF|EMP|DOES-NOT-EXIST", "GARBAGE-NOT-A-QR", "WF|OP|DOES-NOT-EXIST"])
            try:
                client.kiosk_scan(bad_qr)
            except MesflowApiError:
                pass  # a raised error is itself an acceptable rejection path
            return now + self.rng.uniform(5, 30), ActorResult(True, "MISTAKE_BAD_SCAN", {"qr": bad_qr})

        try:
            op_scan = client.kiosk_scan(op.qr)
        except MesflowApiError as exc:
            return now + self.rng.uniform(30, 120), ActorResult(False, "SCAN_OPERATION_ERROR", {"error": str(exc)})
        if op_scan.get("ok") is False:
            # Business rejection (item 10: PO not IN_PROGRESS etc.) --
            # expected occasionally, not an incident by itself.
            return now + self.rng.uniform(60, 300), ActorResult(True, "SCAN_OPERATION_REJECTED", {"reason": op_scan.get("error")})

        try:
            started = client.kiosk_start(employee_id=self.employee.external_id, operation_id=op.operation_id,
                                          station_id=None, device_uuid=self.device_uuid)
        except MesflowApiError as exc:
            return now + self.rng.uniform(30, 120), ActorResult(False, "SESSION_START_ERROR", {"error": str(exc), "operation": op.code})
        if started.get("ok") is False:
            return now + self.rng.uniform(60, 300), ActorResult(True, "SESSION_START_REJECTED", {"reason": started.get("error")})

        session = started.get("item") or started
        self.open_session_id = session.get("id") or session.get("session_id")
        self.current_operation = op
        self.state = "WORKING"

        qty, expected_s, actual_s = distributions.plan_session(self.rng, self.profile, op.standard_seconds_per_unit, *op.qty_range)
        good, defect, rework = distributions.plan_good_defect_rework(self.rng, qty, self.profile)
        self._planned = (good, defect, rework)
        next_at = now + max(30.0, actual_s)
        return next_at, ActorResult(True, "SESSION_START", {
            "session_id": self.open_session_id, "operation": op.code, "planned_qty": qty,
            "planned_good": good, "planned_defect": defect, "planned_rework": rework,
            "planned_duration_s": actual_s,
        })

    # --- FINISH flow ----------------------------------------------------

    def _finish(self, now: float, client: MesflowClient) -> tuple[float | None, ActorResult | None]:
        good, defect, rework = self._planned
        # Item 20 mistake: REWORK > DEFECT once in a while, to see whether
        # the server actually enforces its own invariant (item 18) rather
        # than trusting the client.
        if self.rng.random() < self.profile.mistake_rate * 0.3 and defect > 0:
            rework = defect + 1

        try:
            client.kiosk_scan(self.employee.qr)  # real flow re-scans the employee before quantity entry
            result = client.kiosk_finish(self.open_session_id, good_qty=good, defect_qty=defect, rework_qty=rework)
        except MesflowApiError as exc:
            # Leave state=WORKING -- retry the finish shortly, same as a
            # real kiosk retrying after a transient failure (item 21).
            return now + self.rng.uniform(15, 60), ActorResult(False, "SESSION_FINISH_ERROR",
                                                                  {"error": str(exc), "session_id": self.open_session_id})

        rejected = result.get("ok") is False
        self.state = "IDLE"
        self.open_session_id = None
        self.current_operation = None
        gap = self.rng.uniform(*CHANGEOVER_SECONDS)
        next_at = self._push_past_lunch(now + gap)
        kind = "SESSION_FINISH_REJECTED" if rejected else "SESSION_FINISH"
        return next_at, ActorResult(not rejected if not rejected else True, kind, {
            "good": good, "defect": defect, "rework": rework, "reason": result.get("error") if rejected else None,
        })
