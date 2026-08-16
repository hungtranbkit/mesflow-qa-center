from __future__ import annotations

import hashlib
import json
import random
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class Persona(str, Enum):
    WORKER = "WORKER"
    SUPERVISOR = "SUPERVISOR"
    ADMIN = "ADMIN"
    VIEWER = "VIEWER"


@dataclass(frozen=True)
class MistakeRates:
    wrong_scan_rate: float = 0.02
    double_click_rate: float = 0.02
    refresh_rate: float = 0.02
    abandon_rate: float = 0.01
    late_finish_rate: float = 0.01
    wrong_quantity_rate: float = 0.03
    network_drop_rate: float = 0.02
    retry_rate: float = 0.04

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if not 0 <= value <= 1:
                raise ValueError(f"{name} must be between 0 and 1")


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    seed: int
    ordinal: int
    persona: str
    system_state: str
    action: str
    data_variant: str
    timing_variant: str
    network_variant: str
    concurrency_variant: str
    mistakes: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["mistakes"] = list(self.mistakes)
        return data

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Scenario":
        data = dict(value)
        data["mistakes"] = tuple(data.get("mistakes") or ())
        return cls(**data)

    @property
    def fingerprint(self) -> str:
        raw = json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


PERSONA_ACTIONS = {
    Persona.WORKER: ("SCAN_EMPLOYEE", "SCAN_OPERATION", "START_SESSION", "FINISH_SESSION", "CORRECT_QUANTITY"),
    Persona.SUPERVISOR: ("FILTER_PO", "INSPECT_OPEN_SESSION", "CORRECT_SESSION", "HANDLE_EXCEPTION", "CHECK_WIP"),
    Persona.ADMIN: ("CREATE_TEMPLATE", "CLONE_TEMPLATE", "CREATE_PO", "START_PO", "IMPORT_DATA", "CHANGE_PERMISSION"),
    Persona.VIEWER: ("VIEW_DASHBOARD", "FILTER_REPORT", "SORT_REPORT", "DRILL_DOWN", "COMPARE_SURFACES"),
}

SYSTEM_STATES = ("EMPTY", "PO_DRAFT", "PO_READY", "PO_RUNNING", "PO_COMPLETED", "SESSION_OPEN", "WIP_BLOCKED")
DATA_VARIANTS = ("NORMAL", "ZERO", "NEGATIVE", "VERY_LARGE", "DEFECT", "REPAIRABLE", "DUPLICATE", "MALFORMED")
TIMING_VARIANTS = ("NORMAL", "11:29_TO_11:31", "12:59_TO_13:01", "CROSS_SHIFT", "CROSS_MIDNIGHT", "FUTURE_TIMESTAMP")
NETWORK_VARIANTS = ("ONLINE", "SLOW", "TIMEOUT", "HTTP_429", "HTTP_500", "HTTP_502", "HTTP_503", "OFFLINE_RECONNECT")
CONCURRENCY_VARIANTS = ("SINGLE", "SAME_EMPLOYEE_2_KIOSKS", "SAME_OPERATION_5", "SAME_OPERATION_22", "CONCURRENT_WIP")


class ScenarioGenerator:
    """Generates reproducible behavioral combinations without global randomness."""

    def __init__(self, seed: int, rates: MistakeRates | None = None):
        self.seed = int(seed)
        self.rates = rates or MistakeRates()
        self._rng = random.Random(self.seed)
        self._ordinal = 0

    def generate(self, persona: Persona | str | None = None) -> Scenario:
        self._ordinal += 1
        selected = Persona(persona) if persona else self._rng.choice(tuple(Persona))
        action = self._rng.choice(PERSONA_ACTIONS[selected])
        mistakes = tuple(name.removesuffix("_rate").upper() for name, rate in asdict(self.rates).items() if self._rng.random() < rate)
        dimensions = {
            "system_state": self._rng.choice(SYSTEM_STATES),
            "data_variant": self._rng.choice(DATA_VARIANTS),
            "timing_variant": self._rng.choice(TIMING_VARIANTS),
            "network_variant": self._rng.choice(NETWORK_VARIANTS),
            "concurrency_variant": self._rng.choice(CONCURRENCY_VARIANTS),
        }
        stem = f"{selected.value}_{action}_{self._ordinal:04d}"
        return Scenario(stem, self.seed, self._ordinal, selected.value, action=action, mistakes=mistakes, **dimensions)

    def generate_many(self, count: int, persona: Persona | str | None = None) -> list[Scenario]:
        if count < 0:
            raise ValueError("count must not be negative")
        return [self.generate(persona) for _ in range(count)]
