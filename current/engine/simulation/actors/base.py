from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ActorResult:
    """One action's outcome, for run_manager to log/checkpoint/incident-report."""
    ok: bool
    kind: str                       # e.g. "SESSION_START", "SESSION_FINISH", "MISTAKE_REJECTED", "DASHBOARD_READ"
    detail: dict[str, Any]
    is_business_event: bool = False  # counts toward item 31's "business requests/min" bucket, not heartbeat/reads
