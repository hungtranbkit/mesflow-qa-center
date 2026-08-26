"""Supervisor/Manager actors (items 14, 49): periodic real dashboard/report
reads on a persistent logged-in session, never a business event, never
polled every second."""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timezone

from ..mesflow_client import MesflowApiError, MesflowClient
from .base import ActorResult

SUPERVISOR_INTERVAL_S = (30, 120)
MANAGER_INTERVAL_S = (120, 600)

_SUPERVISOR_READS = ("dashboard", "production_orders", "exceptions")


@dataclass
class WebUserActor:
    actor_id: str
    role: str  # "SUPERVISOR" | "MANAGER"
    username: str
    password: str
    rng: random.Random
    _logged_in: bool = False

    def act(self, now: float, client: MesflowClient, speed: float = 1.0) -> tuple[float | None, ActorResult | None]:
        """`speed` (item 72): a human supervisor/manager reads the
        dashboard on a REAL-world cadence, never compressed by simulation
        acceleration -- see KioskDeviceActor.act's docstring for the exact
        mechanism (next_action_at = now + real_interval * speed)."""
        if not self._logged_in:
            try:
                client.login(self.username, self.password)
                self._logged_in = True
            except MesflowApiError as exc:
                return now + 300 * speed, ActorResult(False, "WEB_LOGIN_ERROR", {"error": str(exc)})

        interval = SUPERVISOR_INTERVAL_S if self.role == "SUPERVISOR" else MANAGER_INTERVAL_S
        choice = self.rng.choice(_SUPERVISOR_READS)
        try:
            if choice == "dashboard":
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                client.dashboard_shift(shift_date=today, shift_id=1)
            elif choice == "production_orders":
                client.production_orders()
            else:
                client.exceptions()
        except MesflowApiError as exc:
            return now + self.rng.uniform(*interval) * speed, ActorResult(False, "WEB_READ_ERROR", {"error": str(exc), "read": choice})

        return now + self.rng.uniform(*interval) * speed, ActorResult(True, "WEB_READ", {"read": choice, "role": self.role})
