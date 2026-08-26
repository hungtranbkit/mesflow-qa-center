from __future__ import annotations

import json
import uuid
from typing import Any

from .store import connect, now


DEFAULT_POLICY = {
    "key": "MESFLOW_STRONG_V1",
    "version": "1.0.0",
    "required_suites": [
        "build_integrity", "critical_unit", "api_contract", "integration", "mes_workflows",
        "ui_critical", "kiosk_emulator", "upgrade", "recovery", "load_soak",
        "test_deployment", "post_deploy_smoke",
    ],
    "block_flaky_layers": ["unit", "api", "integration", "workflow", "browser", "kiosk"],
    "max_invariant_failures": 0,
    "require_hil_when_configured": True,
}


def evaluate(artifact_id: str, environment_id: str, run_ids: list[str],
             policy: dict[str, Any] | None = None, *, hil_configured: bool = False) -> dict[str, Any]:
    policy = dict(policy or DEFAULT_POLICY)
    conn = connect()
    placeholders = ",".join("?" for _ in run_ids) or "NULL"
    suites = [dict(r) for r in conn.execute(
        f"SELECT s.* FROM qa_suite_runs s JOIN qa_qualification_runs q ON q.id=s.qualification_run_id "
        f"WHERE q.id IN ({placeholders}) AND q.artifact_id=? AND q.environment_id=?",
        (*run_ids, artifact_id, environment_id),
    ).fetchall()]
    latest = {s["suite_key"]: s for s in suites}
    missing = [key for key in policy["required_suites"] if key not in latest]
    failed = [key for key, suite in latest.items() if suite["required"] and suite["status"] != "PASSED"]
    flaky = [key for key, suite in latest.items()
             if suite["status"] == "FLAKY" and suite["layer"] in policy["block_flaky_layers"]]
    invariant_failures = conn.execute(
        f"SELECT COUNT(*) n FROM qa_invariant_results WHERE qualification_run_id IN ({placeholders}) AND status='FAILED'",
        tuple(run_ids),
    ).fetchone()["n"] if run_ids else 0
    # esp_hil is intentionally NOT in required_suites (it's only mandatory
    # when the caller explicitly asserts hil_configured=True -- a real
    # certification rig, not every developer's laptop). When it IS
    # required, presence alone used to be enough here, which meant a real
    # esp_hil row that came back BLOCKED/FAILED/NOT_CONFIGURED would not
    # actually block eligibility -- fixed to require suite_run.status ==
    # PASSED specifically, the same bar every other required suite meets.
    hil_status = latest.get("esp_hil", {}).get("status")
    hil_missing = bool(policy.get("require_hil_when_configured") and hil_configured and hil_status != "PASSED")
    eligible = not missing and not failed and not flaky and invariant_failures <= policy["max_invariant_failures"] and not hil_missing
    decision = {"policy": policy, "missing_suites": missing, "failed_suites": failed,
                "critical_flaky_suites": flaky, "invariant_failures": invariant_failures,
                "hil_required_missing": hil_missing, "hil_status": hil_status, "production_eligible": eligible,
                "human_approval_still_required": True}
    certification_id = f"cert-{uuid.uuid4().hex}"
    conn.execute(
        """INSERT INTO qa_certifications(id,artifact_id,environment_id,policy_key,policy_version,status,
           production_eligible,qualification_run_ids,decision_json,certified_at) VALUES(?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(artifact_id,environment_id,policy_key,policy_version) DO UPDATE SET
           status=excluded.status,production_eligible=excluded.production_eligible,
           qualification_run_ids=excluded.qualification_run_ids,decision_json=excluded.decision_json,
           certified_at=excluded.certified_at,invalidated_at=NULL""",
        (certification_id, artifact_id, environment_id, policy["key"], policy["version"],
         "CERTIFIED" if eligible else "FAILED", int(eligible), json.dumps(run_ids), json.dumps(decision, sort_keys=True), now()),
    )
    conn.commit()
    return decision
