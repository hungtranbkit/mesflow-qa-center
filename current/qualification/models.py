from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


RUN_STATES = {"NOT_TESTED", "RUNNING", "FAILED", "BLOCKED", "FLAKY", "PASSED", "CERTIFIED"}
TERMINAL_STATES = RUN_STATES - {"NOT_TESTED", "RUNNING"}


@dataclass(frozen=True)
class Step:
    action: str
    arguments: dict[str, Any] = field(default_factory=dict)
    expected: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Scenario:
    key: str
    version: str
    feature_keys: tuple[str, ...]
    steps: tuple[Step, ...]
    invariants: tuple[str, ...] = ()


@dataclass(frozen=True)
class StepResult:
    ok: bool
    actual: dict[str, Any] = field(default_factory=dict)
    evidence: tuple[str, ...] = ()
    error: str = ""


class Driver(Protocol):
    name: str

    def execute(self, step: Step, context: dict[str, Any]) -> StepResult: ...
