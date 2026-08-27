"""Scenario-level replay (spec section 9): rerun exactly one scenario from
a prior qualification run, against a FRESH isolated deployment of the
SAME artifact bytes and the SAME fixture version the original scenario
ran under -- never the original run's own now-possibly-mutated database,
never a different fixture. The original run/scenario rows are never
touched; qa_replays links the two afterward.

Suite-level replay already exists (qualification/cli.py's `replay-suite`).
This is the finer-grained sibling: it looks up which suite the ORIGINAL
scenario belonged to (via its own suite_run.suite_key) and reruns only
that suite's real work -- the individual scenario runners in live.py are
grouped methods (api_contracts/integration/workflows/invariants), not
independently invocable per scenario key, so "only that scenario" means
"only the minimal real suite that produces it", with the report scoped
down to that one scenario's outcome specifically.
"""
from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from typing import Any

from .database import DeterministicDatabase
from .deployment import ArtifactDeployment, DeploymentError
from .live import LiveMESFlowQualification
from .service import QualificationError, QualificationService
from .store import connect, now

SUITE_KEY_TO_LIVE_METHOD = {"api_contract": "api_contracts", "integration": "integration", "mes_workflows": "workflows"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def replay_scenario(scenario_run_id: str, evidence_root: Path, *, keep_environment: bool = False) -> dict[str, Any]:
    conn = connect()
    original = conn.execute(
        """SELECT sr.*, s.suite_key, s.qualification_run_id, q.artifact_id, q.dataset_version,
           a.sha256 artifact_sha256, a.source_path, a.filename
           FROM qa_scenario_runs sr JOIN qa_suite_runs s ON s.id=sr.suite_run_id
           JOIN qa_qualification_runs q ON q.id=s.qualification_run_id
           JOIN qa_artifacts a ON a.id=q.artifact_id WHERE sr.id=?""", (scenario_run_id,)).fetchone()
    if not original:
        raise QualificationError(f"unknown scenario_run_id: {scenario_run_id}")
    original = dict(original)

    if original["suite_key"] == "ui_critical":
        method_name = None  # dispatched separately below (browser suite)
    else:
        method_name = SUITE_KEY_TO_LIVE_METHOD.get(original["suite_key"])
        if not method_name:
            raise QualificationError(f"replay-scenario does not know how to replay suite_key={original['suite_key']!r} "
                                     f"(supported: {sorted(SUITE_KEY_TO_LIVE_METHOD)} + ui_critical)")

    artifact_path = Path(original["source_path"])
    if not artifact_path.is_file():
        raise DeploymentError(f"BLOCKED: original artifact file no longer exists on disk: {artifact_path}")
    current_sha = _sha256(artifact_path)
    if current_sha != original["artifact_sha256"]:
        raise DeploymentError(f"BLOCKED: artifact bytes at {artifact_path} no longer match the sha256 the "
                              f"original run qualified ({current_sha} != {original['artifact_sha256']}) -- "
                              f"refusing to replay against a different artifact than the one that actually failed")

    service = QualificationService()
    environment = service.attest_environment(
        name=f"replay-scenario-{original['artifact_sha256'][:12]}-{uuid.uuid4().hex[:8]}", kind="QA",
        target_url="http://qualification.invalid", database_identity="pending-isolated-postgresql",
        destructive_allowed=True, identity=f"QA:replay-scenario:{original['artifact_sha256']}:{uuid.uuid4().hex[:8]}")
    replay_run = service.start_run(artifact_id=original["artifact_id"], environment_id=environment["id"],
                                   profile="replay-scenario", dataset_version=original["dataset_version"],
                                   scenario_set_version=original["scenario_version"])

    deployer = ArtifactDeployment(evidence_root)
    deployment = None
    replay_scenario_row = None
    status = "FAILED"
    try:
        deployment = deployer.deploy(replay_run["id"], artifact_path, fixture_version=original["dataset_version"])
        DeterministicDatabase(evidence_root).seed(replay_run["id"], deployment["db_container"], original["dataset_version"])

        if original["suite_key"] == "ui_critical":
            from .browser import run_browser_suite
            suite_result = run_browser_suite(replay_run["id"], deployment["target_url"], evidence_root)
            all_scenarios = suite_result["scenarios"]
        else:
            live = LiveMESFlowQualification(replay_run["id"], deployment["target_url"], deployment["db_container"], evidence_root)
            all_scenarios = getattr(live, method_name)()

        matching = [s for s in all_scenarios if s["key"] == original["scenario_key"]]
        if not matching:
            raise QualificationError(f"replayed suite did not produce a scenario with key {original['scenario_key']!r} "
                                     f"(scenario keys are supposed to be stable across runs of the same artifact -- "
                                     f"this itself is worth investigating)")
        replay_scenario_row = matching[0]
        status = replay_scenario_row["status"]
    finally:
        service.finish_run(replay_run["id"])
        if deployment and not keep_environment:
            deployer.destroy(deployment["namespace"])

    replay_id = f"replay-{uuid.uuid4().hex}"
    conn.execute("""INSERT INTO qa_replays(id,original_scenario_run_id,replay_qualification_run_id,
      replay_scenario_run_id,status,created_at) VALUES(?,?,?,?,?,?)""",
      (replay_id, scenario_run_id, replay_run["id"], replay_scenario_row.get("id") if replay_scenario_row else None,
       status, now()))
    conn.commit()

    return {"replay_id": replay_id, "original_scenario_run_id": scenario_run_id,
           "original_status": original["status"], "original_scenario_key": original["scenario_key"],
           "replay_qualification_run_id": replay_run["id"], "replay_status": status,
           "replay_scenario": replay_scenario_row, "artifact_sha256": original["artifact_sha256"],
           "fixture_version": original["dataset_version"]}


def replays_for(scenario_run_id: str) -> list[dict[str, Any]]:
    conn = connect()
    return [dict(row) for row in conn.execute(
        "SELECT * FROM qa_replays WHERE original_scenario_run_id=? ORDER BY created_at DESC", (scenario_run_id,))]
