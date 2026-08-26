"""Shared Integrity Runner (architecture consolidation phase, spec section
8). The authoritative MES domain invariant set -- derived from the real
work_sessions/quantity_movements/employees/operations model MESFlow's own
API contract enforces -- lived as three independently hand-rolled copies
before this: qualification/live.py's own invariants() (the fullest one,
5 checks, persisted to qa_invariant_results), recovery.py's _invariants_ok
(a weaker 3-check subset, never persisted -- only ever raised locally),
and load_soak.py's inline queries (2 ad hoc checks, also never persisted).
All three now share this one module and the one, full check set -- so
recovery/soak get the SAME invariant coverage live.py's normal workflow
suite already proves, and every invariant check (not just violations)
is now persisted as real evidence, from every caller.
"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from .database import DeterministicDatabase
from .evidence import EvidenceStore
from .store import connect, now

# The authoritative set. Kept as one dict so there is exactly one place to
# add a new domain invariant -- every caller (live.py's mes_workflows
# suite, recovery.py's fault-injection scenarios, load_soak.py's post-
# window check) gets it automatically.
INVARIANT_CHECKS: dict[str, str] = {
    "ONE_ACTIVE_SESSION_PER_EMPLOYEE":
        "SELECT employee_id,count(*) n FROM work_sessions WHERE status='OPEN' GROUP BY employee_id HAVING count(*)>1",
    "NON_NEGATIVE_QUANTITIES":
        "SELECT id,good_qty,defect_qty,rework_qty FROM work_sessions "
        "WHERE good_qty<0 OR defect_qty<0 OR rework_qty<0 OR rework_qty>defect_qty",
    "SESSION_TEMPORAL_ORDER":
        "SELECT id,started_at,ended_at FROM work_sessions WHERE ended_at IS NOT NULL AND ended_at<started_at",
    "NO_DUPLICATE_QUANTITY_MOVEMENT":
        "SELECT correlation_id,session_id,movement_type,delta,count(*) n FROM quantity_movements "
        "WHERE correlation_id<>'' GROUP BY correlation_id,session_id,movement_type,delta HAVING count(*)>1",
    "REFERENTIAL_INTEGRITY":
        "SELECT ws.id FROM work_sessions ws LEFT JOIN employees e ON e.id=ws.employee_id "
        "LEFT JOIN operations o ON o.id=ws.operation_id WHERE e.id IS NULL OR o.id IS NULL",
}


class IntegrityRunner:
    def __init__(self, evidence_root: Path):
        self.conn = connect()
        self.evidence = EvidenceStore(evidence_root)
        self.evidence_root = evidence_root

    def check(self, run_id: str, db_container: str, *, scenario_run_id: str | None = None,
              checks: dict[str, str] | None = None, persist_evidence: bool = True,
              evidence_name: str = "domain-invariants") -> dict[str, Any]:
        """Runs every invariant, persists EVERY result (pass or fail) to
        qa_invariant_results -- not just violations -- so a completed
        run's invariant history is always inspectable, never only
        inferable from whether something raised."""
        database = DeterministicDatabase(self.evidence_root)
        checks = checks if checks is not None else INVARIANT_CHECKS
        results: dict[str, Any] = {}
        for key, sql in checks.items():
            violations = database.query_json(db_container, sql)
            status = "PASSED" if not violations else "FAILED"
            self.conn.execute("""INSERT INTO qa_invariant_results(qualification_run_id,scenario_run_id,invariant_key,
              status,expected_json,actual_json,created_at) VALUES(?,?,?,?,?,?,?)""",
              (run_id, scenario_run_id, key, status, "[]", __import__("json").dumps(violations, default=str), now()))
            results[key] = {"status": status, "violations": violations}
        self.conn.commit()
        if persist_evidence:
            self.evidence.write_json(run_id, f"{evidence_name}-{uuid.uuid4().hex[:8]}.json", results, kind="INVARIANT_EVIDENCE")
        return results

    def assert_ok(self, run_id: str, db_container: str, *, context: str = "", **kwargs: Any) -> dict[str, Any]:
        """Convenience for callers (recovery.py, load_soak.py) that treat
        any violation as a hard failure of whatever they're doing, rather
        than just wanting the recorded results (live.py's own
        mes_workflows suite wants the latter -- it records the check but
        lets the suite's overall PASSED/FAILED aggregation decide, since a
        single scenario's invariant check shouldn't necessarily abort
        every other scenario in the same suite)."""
        results = self.check(run_id, db_container, **kwargs)
        broken = {key: value["violations"] for key, value in results.items() if value["status"] == "FAILED"}
        if broken:
            prefix = f"{context}: " if context else ""
            raise AssertionError(f"{prefix}invariant violations: {broken}")
        return results
