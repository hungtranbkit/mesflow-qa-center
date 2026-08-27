from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .clock import Clock, SystemClock
from .evidence import EvidenceStore
from .service import QualificationError


class FixtureInitializationError(QualificationError):
    pass


class DeterministicDatabase:
    def __init__(self, evidence_root: Path):
        self.evidence = EvidenceStore(evidence_root)
        self.fixture_root = Path(__file__).with_name("fixture_data")

    def _psql(self, container: str, sql: str) -> str:
        result = subprocess.run(["docker", "exec", "-i", container, "psql", "-v", "ON_ERROR_STOP=1",
                                 "-U", "mesflow_qa", "-d", "mesflow_qa", "-At"],
                                input=sql, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if result.returncode:
            raise FixtureInitializationError(result.stdout[-3000:])
        return result.stdout.strip()

    def seed(self, run_id: str, db_container: str, fixture_version: str, *, clock: Clock | None = None) -> dict[str, Any]:
        if fixture_version != "mesflow-fixture-v1":
            raise FixtureInitializationError(f"unsupported fixture version: {fixture_version}")
        # The QA clock abstraction (spec section 5): the fixture's own
        # "late"/"waiting" PO states are encoded as CURRENT_DATE-relative
        # SQL (deterministic, no real waiting needed) -- this independently
        # re-derives what "late" and "waiting" ought to mean against an
        # explicit "now" (real SystemClock in production use; a caller can
        # inject a VirtualClock here to prove the SAME fixture reads as
        # "late"/"waiting" differently at a different simulated instant,
        # without ever touching MESFlow's own runtime clock -- MESFlow's
        # app process always uses real wall-clock time, this only ever
        # governs how THIS check interprets the fixture's dates).
        clock = clock or SystemClock()
        now = clock.now()
        fixture = self.fixture_root / f"{fixture_version}.sql"
        self._psql(db_container, fixture.read_text(encoding="utf-8"))
        query = """SELECT json_build_object(
          'fixture_version','mesflow-fixture-v1',
          'employees',(SELECT count(*) FROM employees WHERE employee_no LIKE 'QA-%'),
          'production_orders',(SELECT count(*) FROM production_orders WHERE code LIKE 'QA-PO-%'),
          'parts',(SELECT count(*) FROM parts WHERE code LIKE 'QA-PO-%-PART'),
          'operations',(SELECT count(*) FROM operations WHERE code LIKE 'QA-PO-%-OP'),
          'active_sessions',(SELECT count(*) FROM work_sessions ws JOIN employees e ON e.id=ws.employee_id WHERE e.employee_no LIKE 'QA-%' AND ws.status='OPEN'),
          'closed_sessions',(SELECT count(*) FROM work_sessions ws JOIN employees e ON e.id=ws.employee_id WHERE e.employee_no LIKE 'QA-%' AND ws.status='CLOSED'),
          'migration_head',(SELECT version_num FROM alembic_version),
          'database',current_database(),
          'po_late_due_date',(SELECT due_date FROM production_orders WHERE code='QA-PO-LATE'),
          'po_wait_due_date',(SELECT due_date FROM production_orders WHERE code='QA-PO-WAIT'),
          'po_late_status',(SELECT status FROM production_orders WHERE code='QA-PO-LATE'),
          'po_wait_status',(SELECT status FROM production_orders WHERE code='QA-PO-WAIT'))::text;"""
        output = self._psql(db_container, query)
        try:
            facts = json.loads(output)
        except Exception as exc:
            raise FixtureInitializationError(f"fixture verification returned invalid JSON: {output}") from exc
        expected = {"employees": 3, "production_orders": 4, "parts": 4, "operations": 4,
                    "active_sessions": 1, "closed_sessions": 1, "database": "mesflow_qa"}
        mismatches = {key: {"expected": value, "actual": facts.get(key)} for key, value in expected.items() if facts.get(key) != value}

        clock_checks: dict[str, Any] = {}
        for po_key, due_key, expect_before_now in (("po_late", "po_late_due_date", True), ("po_wait", "po_wait_due_date", False)):
            raw = facts.get(due_key)
            if not raw:
                clock_checks[po_key] = {"error": f"{due_key} missing from fixture facts"}
                continue
            due_date = datetime.fromisoformat(str(raw)).replace(tzinfo=timezone.utc)
            is_before_now = due_date < now
            clock_checks[po_key] = {"due_date": raw, "now": now.isoformat(), "is_before_now": is_before_now,
                                    "expected_before_now": expect_before_now, "ok": is_before_now == expect_before_now}
        clock_mismatches = {k: v for k, v in clock_checks.items() if not v.get("ok", False)}

        evidence = self.evidence.write_json(run_id, "fixture-verification.json",
                                            {"fixture_version": fixture_version, "facts": facts, "mismatches": mismatches,
                                             "clock_checks": clock_checks}, kind="FIXTURE_VERIFICATION")
        if mismatches:
            raise FixtureInitializationError(f"fixture sanity mismatch: {mismatches}")
        if clock_mismatches:
            raise FixtureInitializationError(f"fixture time-state does not match the clock's 'now' as expected: {clock_mismatches}")
        return {"fixture_version": fixture_version, "facts": facts, "clock_checks": clock_checks, "evidence": evidence}

    def query_json(self, db_container: str, sql: str) -> Any:
        return json.loads(self._psql(db_container, f"SELECT COALESCE(json_agg(q),'[]'::json)::text FROM ({sql}) q;"))
