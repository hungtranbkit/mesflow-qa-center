from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class InvalidTransition(ValueError):
    pass


class POState(str, Enum):
    DRAFT = "DRAFT"
    READY = "READY"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class OperationState(str, Enum):
    NOT_STARTED = "NOT_STARTED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class SessionState(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


PO_TRANSITIONS = {
    POState.DRAFT: {POState.READY, POState.CANCELLED},
    POState.READY: {POState.RUNNING, POState.CANCELLED},
    POState.RUNNING: {POState.COMPLETED, POState.CANCELLED},
    POState.COMPLETED: set(),
    POState.CANCELLED: set(),
}
OPERATION_TRANSITIONS = {
    OperationState.NOT_STARTED: {OperationState.RUNNING, OperationState.CANCELLED},
    OperationState.RUNNING: {OperationState.COMPLETED, OperationState.CANCELLED},
    OperationState.COMPLETED: set(),
    OperationState.CANCELLED: set(),
}
SESSION_TRANSITIONS = {SessionState.OPEN: {SessionState.CLOSED}, SessionState.CLOSED: set()}


def transition(current: Enum, target: Enum, graph: dict[Enum, set[Enum]]) -> Enum:
    if target not in graph[current]:
        raise InvalidTransition(f"invalid transition {current.value} -> {target.value}")
    return target


@dataclass(frozen=True)
class Quantity:
    good: int = 0
    defect: int = 0
    repairable: int = 0

    def validate(self) -> None:
        if min(self.good, self.defect, self.repairable) < 0:
            raise ValueError("quantity must not be negative")
        if self.repairable > self.defect:
            raise ValueError("repairable quantity must not exceed defect quantity")

    def __add__(self, other: "Quantity") -> "Quantity":
        value = Quantity(self.good + other.good, self.defect + other.defect, self.repairable + other.repairable)
        value.validate()
        return value
