from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable


def evaluate_domain_snapshot(snapshot: dict[str, Iterable[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Evaluate qualification invariants against a transport-neutral MES snapshot."""
    failures: list[dict[str, Any]] = []
    sessions = list(snapshot.get("sessions", []))
    operations = list(snapshot.get("operations", []))

    active_by_employee: dict[str, list[str]] = {}
    for session in sessions:
        sid = str(session.get("id", "unknown"))
        employee = str(session.get("employee_id", ""))
        if session.get("status") in {"ACTIVE", "OPEN", "IN_PROGRESS"}:
            active_by_employee.setdefault(employee, []).append(sid)
        good = session.get("good_qty", 0) or 0
        defect = session.get("defect_qty", 0) or 0
        if good < 0 or defect < 0:
            failures.append({"key": "NON_NEGATIVE_QUANTITY", "entity_id": sid, "actual": {"good": good, "defect": defect}})
        started, ended = session.get("started_at"), session.get("ended_at")
        if ended and started and datetime.fromisoformat(ended) < datetime.fromisoformat(started):
            failures.append({"key": "SESSION_TEMPORAL_ORDER", "entity_id": sid, "actual": {"started_at": started, "ended_at": ended}})

    for employee, ids in active_by_employee.items():
        if employee and len(ids) > 1:
            failures.append({"key": "ONE_ACTIVE_SESSION_PER_EMPLOYEE", "entity_id": employee, "actual": ids})

    for operation in operations:
        produced = operation.get("produced_qty", 0) or 0
        target = operation.get("target_qty", 0) or 0
        if produced < 0 or target < 0 or (target and produced > target and not operation.get("overproduction_allowed", False)):
            failures.append({"key": "OPERATION_PROGRESS_BOUNDS", "entity_id": str(operation.get("id", "unknown")),
                             "actual": {"produced_qty": produced, "target_qty": target}})
        if operation.get("status") == "COMPLETED" and target and produced < target:
            failures.append({"key": "COMPLETED_OPERATION_CONSTRAINT", "entity_id": str(operation.get("id", "unknown")),
                             "actual": {"produced_qty": produced, "target_qty": target}})
    return failures
