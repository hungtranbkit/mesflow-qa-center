"""Periodic dashboard/session reconciliation (items 15, 62, 63).

Deliberately reads ONLY the same public dashboard API a real supervisor's
browser calls (item 89) -- no direct DB query. This checks the dashboard's
own internal consistency: per-operation daily GOOD/DEFECT totals
(`items[].day_good_qty/day_defect_qty`) must equal the sum of that same
day's CLOSED `sessions[]` rows for that operation. A mismatch here means
the dashboard's aggregation disagrees with the session rows it was itself
built from -- exactly the class of bug item 15 exists to catch.

Every failure is reported through the EXISTING bug_store (item 38's
dedup/occurrence-tracking is already implemented there -- this module adds
no parallel incident system), tagged so simulation-sourced records are
never confused with Functional QA's own bug records (item 42).
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from .. import bug_store
from .mesflow_client import MesflowApiError, MesflowClient

FEATURE_DASHBOARD_RECONCILIATION = "SIM:dashboard_reconciliation"


def check_dashboard_reconciliation(client: MesflowClient, *, shift_date: str, shift_id: int, run_id: str) -> dict[str, Any]:
    try:
        data = client.dashboard_shift(shift_date=shift_date, shift_id=shift_id)
    except MesflowApiError as exc:
        return {"ok": False, "error": str(exc), "checked_operations": 0, "mismatches": 0}

    sessions = data.get("sessions") or []
    items = data.get("items") or []
    by_op_good: dict[int, int] = defaultdict(int)
    by_op_defect: dict[int, int] = defaultdict(int)
    for s in sessions:
        if s.get("session_status") not in ("CLOSED", None):
            continue
        op_id = s.get("operation_id")
        if op_id is None:
            continue
        by_op_good[op_id] += int(s.get("good_qty") or 0)
        by_op_defect[op_id] += int(s.get("defect_qty") or 0)

    mismatches = 0
    for row in items:
        op_id = row.get("operation_id") or row.get("id")
        if op_id is None:
            continue
        expected_good = by_op_good.get(op_id, 0)
        expected_defect = by_op_defect.get(op_id, 0)
        actual_good = int(row.get("day_good_qty") or 0)
        actual_defect = int(row.get("day_defect_qty") or 0)
        if expected_good != actual_good or expected_defect != actual_defect:
            mismatches += 1
            bug_store.record_bug(
                feature=FEATURE_DASHBOARD_RECONCILIATION,
                error_type="DASHBOARD_SESSION_RECONCILIATION_FAILED",
                title=f"Operation {row.get('operation_code') or op_id}: dashboard day_good/defect_qty disagrees with its own CLOSED sessions",
                severity="ERROR",
                expected=f"good={expected_good} defect={expected_defect}",
                actual=f"good={actual_good} defect={actual_defect}",
                api="/api/dashboard/shift",
                evidence={"source": "QA_SIMULATION", "run_id": run_id, "operation_id": op_id,
                          "shift_date": shift_date, "shift_id": shift_id},
                run_id=run_id,
            )

    return {"ok": True, "checked_operations": len(items), "mismatches": mismatches}


def check_active_worker_reconciliation(client: MesflowClient, *, shift_date: str, shift_id: int, run_id: str,
                                        expected_open_employee_ids: set[int]) -> dict[str, Any]:
    """Item 63: independently-tracked (by the simulation itself, from each
    EmployeeActor's own state) set of employees with an OPEN session,
    compared against what the dashboard reports as currently working."""
    try:
        data = client.dashboard_shift(shift_date=shift_date, shift_id=shift_id)
    except MesflowApiError as exc:
        return {"ok": False, "error": str(exc)}
    sessions = data.get("sessions") or []
    dashboard_open = {s.get("employee_id") for s in sessions if s.get("session_status") == "OPEN"}
    dashboard_open.discard(None)
    mismatch = dashboard_open.symmetric_difference(expected_open_employee_ids)
    if mismatch:
        bug_store.record_bug(
            feature="SIM:active_worker_reconciliation",
            error_type="QA_DATA_RECONCILIATION_ACTIVE_WORKERS",
            title="Dashboard's currently-working employee set disagrees with the simulation's own tracked OPEN sessions",
            severity="ERROR",
            expected=str(sorted(expected_open_employee_ids)),
            actual=str(sorted(dashboard_open)),
            api="/api/dashboard/shift",
            evidence={"source": "QA_SIMULATION", "run_id": run_id, "mismatched_employee_ids": sorted(mismatch)},
            run_id=run_id,
        )
    return {"ok": True, "mismatched": len(mismatch)}
