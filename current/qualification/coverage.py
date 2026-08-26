from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

from .store import connect, now

REGISTRY = Path(__file__).with_name("features.json")


def registry() -> dict[str, Any]:
    return json.loads(REGISTRY.read_text(encoding="utf-8"))


def registry_sha256() -> str:
    return hashlib.sha256(REGISTRY.read_bytes()).hexdigest()


def report(run_ids: list[str] | None = None) -> dict[str, Any]:
    specs = registry()["features"]
    conn = connect()
    run_ids = run_ids or []
    placeholders = ",".join("?" for _ in run_ids) or "NULL"
    rows = conn.execute(
        f"""SELECT sr.scenario_key,sr.driver,sr.status,s.layer,s.qualification_run_id
            FROM qa_scenario_runs sr JOIN qa_suite_runs s ON s.id=sr.suite_run_id
            WHERE s.qualification_run_id IN ({placeholders})""", tuple(run_ids)
    ).fetchall() if run_ids else []
    details = []
    for feature in specs:
        matching = [dict(row) for row in rows if row["scenario_key"].startswith(feature["key"])]
        passed_layers = sorted({row["layer"] for row in matching if row["status"] == "PASSED"})
        passed_drivers = sorted({row["driver"] for row in matching if row["status"] == "PASSED"})
        missing_layers = sorted(set(feature["required_layers"]) - set(passed_layers))
        missing_drivers = sorted(set(feature["required_drivers"]) - set(passed_drivers))
        covered = not missing_layers and not missing_drivers
        failures = [row for row in matching if row["status"] in {"FAILED", "BLOCKED", "FLAKY"}]
        state = "COVERED" if covered else ("BLOCKED" if failures else ("PARTIALLY_COVERED" if matching else "UNCOVERED"))
        details.append({**feature, "covered": covered, "passed_layers": passed_layers,
                        "passed_drivers": passed_drivers, "missing_layers": missing_layers,
                        "missing_drivers": missing_drivers, "status": state,
                        "supporting_scenarios": matching, "blocking_results": failures})
    covered = sum(1 for item in details if item["covered"])
    critical = [item for item in details if item["critical"]]
    critical_covered = sum(1 for item in critical if item["covered"])
    return {"schema_version": "1.0.0", "total_features": len(details), "covered_features": covered,
            "uncovered_features": len(details) - covered, "critical_features": len(critical),
            "critical_covered_features": critical_covered,
            "critical_feature_coverage_percent": round(critical_covered * 100 / len(critical), 1) if critical else 100.0,
            "features": details}


def snapshot(run_id: str) -> dict[str, Any]:
    """Compute coverage for exactly one qualification run and persist it
    permanently (qa_coverage_snapshots), independent of the run's ephemeral
    Docker resources or evidence files -- called once, at the end of every
    completed run-artifact/replay-suite/replay-scenario invocation. Coverage
    for an old run is then always re-readable without recomputing anything
    against a scratch database that no longer exists.
    """
    conn = connect()
    run_row = conn.execute(
        "SELECT q.id,a.sha256 artifact_sha256 FROM qa_qualification_runs q JOIN qa_artifacts a ON a.id=q.artifact_id WHERE q.id=?",
        (run_id,)).fetchone()
    if not run_row:
        raise ValueError(f"unknown qualification run: {run_id}")
    result = report([run_id])
    by_state = {"COVERED": 0, "PARTIALLY_COVERED": 0, "UNCOVERED": 0, "BLOCKED": 0}
    for feature in result["features"]:
        by_state[feature["status"]] = by_state.get(feature["status"], 0) + 1
    snapshot_id = f"cov-{uuid.uuid4().hex}"
    reg = registry()
    conn.execute(
        """INSERT INTO qa_coverage_snapshots(id,qualification_run_id,artifact_sha256,registry_version,
           registry_sha256,total_features,critical_features,covered_features,partially_covered_features,
           uncovered_features,blocked_features,critical_covered_features,snapshot_json,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(qualification_run_id) DO UPDATE SET artifact_sha256=excluded.artifact_sha256,
           registry_version=excluded.registry_version,registry_sha256=excluded.registry_sha256,
           total_features=excluded.total_features,critical_features=excluded.critical_features,
           covered_features=excluded.covered_features,partially_covered_features=excluded.partially_covered_features,
           uncovered_features=excluded.uncovered_features,blocked_features=excluded.blocked_features,
           critical_covered_features=excluded.critical_covered_features,snapshot_json=excluded.snapshot_json,
           created_at=excluded.created_at""",
        (snapshot_id, run_id, run_row["artifact_sha256"], reg["schema_version"], registry_sha256(),
         result["total_features"], result["critical_features"], by_state["COVERED"], by_state["PARTIALLY_COVERED"],
         by_state["UNCOVERED"], by_state["BLOCKED"], result["critical_covered_features"],
         json.dumps(result, sort_keys=True, default=str), now()),
    )
    conn.commit()
    return dict(conn.execute("SELECT * FROM qa_coverage_snapshots WHERE id=?", (snapshot_id,)).fetchone())


def read_snapshot(run_id: str) -> dict[str, Any] | None:
    """Read a persisted snapshot back -- works even after the run's Docker
    resources/evidence files are gone; never recomputes."""
    conn = connect()
    row = conn.execute("SELECT * FROM qa_coverage_snapshots WHERE qualification_run_id=?", (run_id,)).fetchone()
    if not row:
        return None
    result = dict(row)
    result["snapshot"] = json.loads(result.pop("snapshot_json"))
    return result
