"""Simulated clock (item 72): decouples "simulated time" (what employee
shift/session logic reasons about) from "real wall-clock time" (what
time.sleep() actually waits). speed=1.0 is real time; speed=10.0 means 10
simulated seconds pass per 1 real second.

Per item 72: acceleration must NOT compress every timer equally into
artificial bursts. Only the PACING of scheduled actions is compressed here
-- each actor still spaces its own actions realistically in SIMULATED
time, so a 10x run still issues the same event density per simulated hour,
just delivered over 1/10th the real wall-clock time. Heartbeat/dashboard
polling actors use their own independent real-time-ish bounds (see
actors/web_user.py, actors/kiosk_device.py) rather than being compressed
1:1 with employee session pacing.
"""
from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class SimClock:
    speed: float = 1.0          # simulated-seconds per real-second
    _origin_real: float = 0.0
    _origin_sim: float = 0.0

    def __post_init__(self) -> None:
        if self.speed <= 0:
            raise ValueError("speed must be > 0")
        self._origin_real = time.time()
        self._origin_sim = self._origin_real  # sim clock starts aligned to real wall-clock

    def now(self) -> float:
        """Current simulated time, as a Unix epoch float."""
        elapsed_real = time.time() - self._origin_real
        return self._origin_sim + elapsed_real * self.speed

    def real_seconds_until(self, sim_target: float) -> float:
        delta_sim = sim_target - self.now()
        return max(0.0, delta_sim / self.speed)

    def to_dict(self) -> dict[str, float]:
        return {"speed": self.speed, "origin_real": self._origin_real, "origin_sim": self._origin_sim}

    @classmethod
    def resume(cls, d: dict[str, float]) -> "SimClock":
        """Re-anchors so simulated time continues from wherever it had
        reached at persist time, effectively PAUSED for the duration QA
        Center was actually down -- no catch-up burst, no skipped time.
        Any actor whose next_action_at fell during the outage simply runs
        that one action immediately on resume (see scheduler.py), then
        paces normally from there -- never a flood."""
        clock = cls(speed=d["speed"])
        clock._origin_real = time.time()
        clock._origin_sim = d["origin_sim"]
        return clock

    def snapshot_for_persist(self) -> dict[str, float]:
        """What resume() needs: speed + the CURRENT simulated time as of
        this exact instant."""
        return {"speed": self.speed, "origin_sim": self.now()}
