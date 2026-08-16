from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from .state_model import Quantity


@dataclass(frozen=True)
class OracleFailure:
    invariant: str
    expected: Any
    actual: Any
    source: str
    relevant_ids: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OracleResult:
    failures: tuple[OracleFailure, ...]
    observations: dict[str, Any]

    @property
    def ok(self) -> bool:
        return not self.failures


class ConsistencyOracle:
    """Compares canonical session sums with API/UI projections at checkpoints."""

    def __init__(self, readers: Mapping[str, Callable[[dict[str, Any]], Any]]):
        self.readers = dict(readers)

    @staticmethod
    def _quantity(row: Mapping[str, Any]) -> Quantity:
        q = Quantity(
            int(row.get("good_qty", row.get("done_qty", row.get("good_quantity", 0))) or 0),
            int(row.get("defect_qty", row.get("defect_quantity", 0)) or 0),
            int(row.get("rework_qty", row.get("repairable_qty", row.get("repairable_quantity", 0))) or 0),
        )
        q.validate()
        return q

    def check_operation(self, context: dict[str, Any]) -> OracleResult:
        observations = {name: reader(context) for name, reader in self.readers.items()}
        sessions = observations.get("sessions") or []
        canonical = Quantity()
        failures: list[OracleFailure] = []
        for row in sessions:
            if str(row.get("status") or "").upper() == "CLOSED":
                try:
                    canonical += self._quantity(row)
                except ValueError as exc:
                    failures.append(OracleFailure("SESSION_QUANTITY_BOUNDS", "non-negative; repairable <= defect", str(exc), "sessions", context))
        for source in ("operation", "dashboard", "report"):
            row = observations.get(source)
            if row is None:
                continue
            try:
                actual = self._quantity(row)
            except ValueError as exc:
                failures.append(OracleFailure("AGGREGATE_QUANTITY_BOUNDS", "non-negative; repairable <= defect", str(exc), source, context))
                continue
            if actual != canonical:
                failures.append(OracleFailure("OPERATION_EQUALS_CLOSED_SESSIONS", canonical, actual, source, context))
        return OracleResult(tuple(failures), observations)

    @staticmethod
    def check_wip(source: Mapping[str, Any], ledgers: list[Mapping[str, Any]]) -> OracleResult:
        supplied = int(source.get("rework_qty") if str(source.get("source_qty_kind") or "GOOD").upper() == "REWORK" else source.get("done_qty") or 0)
        consumed = sum(int(x.get("good_qty_consumed") or 0) + int(x.get("defect_qty_consumed") or 0) for x in ledgers)
        available = supplied - consumed
        failures = () if available >= 0 else (OracleFailure("WIP_NOT_NEGATIVE", ">= 0", available, "input_consumption_ledger"),)
        return OracleResult(failures, {"supplied": supplied, "consumed": consumed, "available": available})
