"""Shared scenario-execution contract (architecture consolidation phase).

Six of the production-policy suite modules (build_integrity, upgrade,
recovery, kiosk_emulator, test_deployment, post_deploy_smoke) each
independently hand-wrote the exact same insert-scenario/run/catch/evidence/
update/insert-attempt bookkeeping, differing only in their scenario_version
string, driver name, and evidence kind. This is the one place that logic
now lives; those modules construct one `ScenarioRunner` in __init__ and
call `.run(...)` instead of duplicating the SQL.

Deliberately NOT used by qualification/live.py: that module's own
`_scenario()` carries extra, load-bearing mechanics (failure fingerprinting
for flaky/dedup tracking, `expected` vs `actual` distinction, per-call
`api_calls` tracing) that the other six modules don't need yet -- forcing
it through this simpler shared contract would mean either losing that
richness or complicating every other caller to carry fields they don't
use. It stays self-contained on purpose (see docs/consolidation notes in
the audit for this phase).
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .evidence import EvidenceStore
from .store import connect, now


class ScenarioRunner:
    def __init__(self, evidence_root: Path, *, scenario_version: str, driver: str, evidence_kind: str):
        self.conn = connect()
        self.evidence = EvidenceStore(evidence_root)
        self.scenario_version = scenario_version
        self.driver = driver
        self.evidence_kind = evidence_kind

    def run(self, suite_id: str, run_id: str, key: str, fn: Callable[[], dict[str, Any] | None], *,
            track_duration: bool = False) -> dict[str, Any]:
        scenario_id = f"scenario-{uuid.uuid4().hex}"
        self.conn.execute("""INSERT INTO qa_scenario_runs(id,suite_run_id,scenario_key,scenario_version,driver,
          status,started_at) VALUES(?,?,?,?,?,'RUNNING',?)""",
          (scenario_id, suite_id, key, self.scenario_version, self.driver, now()))
        self.conn.commit()
        status, actual, first_failure = "PASSED", {}, ""
        started = time.time()
        try:
            actual = fn() or {}
        except Exception as exc:  # noqa: BLE001 -- every failure must become one evidenced FAILED scenario, never a crash
            status, first_failure = "FAILED", "assertion"
            actual = {"error_class": type(exc).__name__, "message": str(exc)}
        if track_duration:
            actual["duration_seconds"] = round(time.time() - started, 3)
        evidence = self.evidence.write_json(run_id, f"{key.replace('.', '-')}.json",
            {"scenario": key, "status": status, "actual": actual}, kind=self.evidence_kind,
            suite_run_id=suite_id, scenario_run_id=scenario_id)
        self.conn.execute("""UPDATE qa_scenario_runs SET status=?,finished_at=?,first_failing_step=?,
          actual_json=? WHERE id=?""", (status, now(), first_failure, json.dumps(actual, default=str), scenario_id))
        self.conn.execute("INSERT INTO qa_attempts(scenario_run_id,attempt_no,status,fingerprint,started_at,finished_at) "
                          "VALUES(?,1,?,?,?,?)", (scenario_id, status, "", now(), now()))
        self.conn.commit()
        return {"id": scenario_id, "key": key, "status": status, "actual": actual, "evidence": evidence}
