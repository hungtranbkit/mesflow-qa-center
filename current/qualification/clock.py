from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    """Production clock. Qualification code uses this unless a clock is injected."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


@dataclass
class VirtualClock:
    """Explicit test-only clock; never selected from ambient environment variables."""

    current: datetime

    def __post_init__(self) -> None:
        if self.current.tzinfo is None:
            raise ValueError("virtual clock requires a timezone-aware datetime")
        self.current = self.current.astimezone(timezone.utc)

    def now(self) -> datetime:
        return self.current

    def advance(self, **delta: float) -> datetime:
        self.current += timedelta(**delta)
        return self.current
