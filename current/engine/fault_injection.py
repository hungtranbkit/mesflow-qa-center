from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass(frozen=True)
class Fault:
    kind: str
    delay_ms: int = 0
    status: int | None = None
    server_received: bool = False


class FaultPlan:
    def __init__(self, seed: int):
        self._rng = random.Random(int(seed))

    def for_variant(self, variant: str) -> Fault:
        variant = variant.upper()
        if variant == "SLOW":
            return Fault(variant, delay_ms=self._rng.randint(500, 5000))
        if variant == "TIMEOUT":
            return Fault(variant, delay_ms=30000, server_received=True)
        if variant.startswith("HTTP_"):
            return Fault(variant, status=int(variant.split("_", 1)[1]))
        if variant == "OFFLINE_RECONNECT":
            return Fault(variant, server_received=False)
        return Fault("ONLINE")
