"""Kiosk device heartbeat actor (item 48): periodic heartbeat with
realistic RSSI/uptime/memory variation. Rare controlled reboot only
(0-2/device/day), never every few minutes."""
from __future__ import annotations

import random
from dataclasses import dataclass, field

from ..mesflow_client import MesflowApiError, MesflowClient
from .base import ActorResult

HEARTBEAT_INTERVAL_S = (20, 40)


@dataclass
class KioskDeviceActor:
    actor_id: str
    device_uuid: str
    rng: random.Random
    uptime_s: float = 0.0
    last_reboot_check_day: int = -1

    def act(self, now: float, client: MesflowClient, speed: float = 1.0) -> tuple[float | None, ActorResult | None]:
        """`speed` (item 72): heartbeat cadence is a REAL-world device
        property, never compressed by simulation acceleration -- the
        returned next_action_at is `now + real_interval * speed` so that,
        once the scheduler divides back by speed to get a real sleep, the
        actual wall-clock gap between heartbeats stays HEARTBEAT_INTERVAL_S
        regardless of how fast employee session pacing is running."""
        day = int(now // 86400)
        if day != self.last_reboot_check_day:
            self.last_reboot_check_day = day
            if self.rng.random() < 0.15:  # averages well under 2/device/day across the fleet
                self.uptime_s = 0.0
        self.uptime_s += self.rng.uniform(*HEARTBEAT_INTERVAL_S)

        status = {
            "rssi": self.rng.randint(-80, -40),
            "uptime_s": int(self.uptime_s),
            "free_heap_bytes": self.rng.randint(80_000, 220_000),
            "firmware_version": "qa-sim-1.0",
        }
        try:
            client.kiosk_heartbeat(self.device_uuid, status)
        except MesflowApiError as exc:
            return now + self.rng.uniform(*HEARTBEAT_INTERVAL_S) * speed, ActorResult(False, "HEARTBEAT_ERROR", {"error": str(exc)})
        return now + self.rng.uniform(*HEARTBEAT_INTERVAL_S) * speed, ActorResult(True, "HEARTBEAT", status)
