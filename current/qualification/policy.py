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

# Policy tiers (spec section 11). MESFLOW_STRONG_V1 (DEFAULT_POLICY) above
# is NEVER modified -- it predates OFFLINE_BURST/MIGRATION/BACKUP_RESTORE/
# ROLLBACK/scheduler_reliability and any existing certification keyed to it
# must keep meaning exactly what it always meant. A release-policy upgrade
# that wants the new suites is a NEW versioned key (MESFLOW_STRONG_V2),
# never a silent edit of v1's required_suites.
FAST_POLICY = {
    "key": "MESFLOW_FAST_V1",
    # 1.0.0 -> 1.1.0: Kiosk Certification phase adds "basic emulator smoke"
    # to FAST per spec section 30. Safe to bump in place (not a new V2 key
    # like STRONG's precedent below) -- evaluate_tier() is read-only and
    # never persists a qa_certifications row, so there is no historical
    # certification whose meaning this could retroactively change.
    "version": "1.1.0",
    # Developer/local iteration: fast, no real Docker sandbox chaos/burst/
    # migration suites, no HIL. Still a real bar (build integrity, unit,
    # full API contract, integration, MES workflows, one UI smoke pass,
    # kiosk emulator smoke, invariants) -- not a rubber stamp.
    "required_suites": ["build_integrity", "critical_unit", "api_contract", "integration",
                        "mes_workflows", "ui_critical", "kiosk_emulator"],
    "block_flaky_layers": ["unit", "api", "integration", "workflow"],
    "max_invariant_failures": 0,
    "require_hil_when_configured": False,
}

STRONG_V2_POLICY = {
    "key": "MESFLOW_STRONG_V2",
    "version": "1.0.0",
    # Everything MESFLOW_STRONG_V1 already required, PLUS the reliability
    # suites this phase actually built and proved real: offline burst,
    # scheduler reliability, migration (suite_key 'upgrade', shared with the
    # plain upgrade path), backup/restore (two suite_keys: it credits
    # 'database' and 'recovery' layers separately), and rollback.
    "required_suites": DEFAULT_POLICY["required_suites"] + [
        "offline_burst", "scheduler_reliability", "backup_restore_database",
        "backup_restore_recovery", "rollback",
    ],
    "block_flaky_layers": DEFAULT_POLICY["block_flaky_layers"],
    "max_invariant_failures": 0,
    "require_hil_when_configured": True,
}

EXTENDED_POLICY = {
    "key": "MESFLOW_EXTENDED_V1",
    "version": "1.0.0",
    # Periodic high-confidence reliability qualification -- everything
    # STRONG_V2 requires, plus real accelerated-or-real-time long-running
    # simulation evidence. Deliberately does NOT require the full
    # real-time NIGHTLY/EXTENDED_3D/7D wall-clock duration for every
    # evaluation (spec: "Do not make multi-day qualification mandatory for
    # every build") -- long_running_factory_simulation PASSING at ANY
    # profile (including the fast SMOKE/RELEASE accelerated ones) is
    # enough to satisfy this tier's suite requirement; whether a
    # particular certification decision was backed by a true full-duration
    # NIGHTLY/EXTENDED run is a separate, honest fact the UI surfaces from
    # qa_suite_runs.command_json (profile name), never blurred into a
    # single PASS/FAIL bit.
    "required_suites": STRONG_V2_POLICY["required_suites"] + ["long_running_factory_simulation"],
    "block_flaky_layers": STRONG_V2_POLICY["block_flaky_layers"],
    "max_invariant_failures": 0,
    "require_hil_when_configured": True,
}

POLICY_TIERS = {"FAST": FAST_POLICY, "STRONG": STRONG_V2_POLICY, "EXTENDED": EXTENDED_POLICY}


def _decide(policy: dict[str, Any], run_ids: list[str], *, hil_configured: bool) -> dict[str, Any]:
    """Shared decision logic between evaluate() (single environment_id,
    persists a qa_certifications row -- the real CERTIFY action) and
    evaluate_tier() (artifact-scoped across every run of that exact SHA,
    read-only preview -- spec section 11's policy-tier comparison). Kept
    as one function so the two never silently diverge on what counts as
    eligible."""
    conn = connect()
    placeholders = ",".join("?" for _ in run_ids) or "NULL"
    suites = [dict(r) for r in conn.execute(
        f"SELECT * FROM qa_suite_runs WHERE qualification_run_id IN ({placeholders})", tuple(run_ids)
    ).fetchall()] if run_ids else []
    # Keep the LATEST attempt per suite_key (by started_at), not whichever
    # row the SQL happened to return last -- matters once a suite_key can
    # legitimately appear in more than one run for the same artifact.
    latest: dict[str, dict[str, Any]] = {}
    for suite in sorted(suites, key=lambda s: s["started_at"]):
        latest[suite["suite_key"]] = suite
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
    return {"policy": policy, "missing_suites": missing, "failed_suites": failed,
            "critical_flaky_suites": flaky, "invariant_failures": invariant_failures,
            "hil_required_missing": hil_missing, "hil_status": hil_status, "production_eligible": eligible,
            "human_approval_still_required": True, "satisfied_suites": sorted(set(latest) & set(policy["required_suites"])),
            "run_ids": run_ids}


def evaluate(artifact_id: str, environment_id: str, run_ids: list[str],
             policy: dict[str, Any] | None = None, *, hil_configured: bool = False) -> dict[str, Any]:
    policy = dict(policy or DEFAULT_POLICY)
    conn = connect()
    placeholders = ",".join("?" for _ in run_ids) or "NULL"
    scoped_run_ids = [r["id"] for r in conn.execute(
        f"SELECT id FROM qa_qualification_runs WHERE id IN ({placeholders}) AND artifact_id=? AND environment_id=?",
        (*run_ids, artifact_id, environment_id)).fetchall()] if run_ids else []
    decision = _decide(policy, scoped_run_ids, hil_configured=hil_configured)
    certification_id = f"cert-{uuid.uuid4().hex}"
    eligible = decision["production_eligible"]
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


def evaluate_tier(tier: str, artifact_sha256: str, *, hil_configured: bool = False) -> dict[str, Any]:
    """Spec section 11 demo F: "Evaluate the same artifact under FAST /
    STRONG / EXTENDED and show the differing requirements/results
    honestly." Read-only (never writes qa_certifications -- that stays the
    single-environment evaluate()/CERTIFY action) and artifact-scoped
    (spec section 10: pools evidence from every qualification run of this
    EXACT artifact SHA256, regardless of which throwaway environment_id
    each individual CLI invocation minted), so FAST/STRONG/EXTENDED can
    each draw on migration/backup-restore/rollback/offline-burst even
    though those are separate CLI invocations with separate environment
    rows."""
    tier = tier.upper()
    if tier not in POLICY_TIERS:
        raise ValueError(f"unknown policy tier: {tier!r} (expected one of {sorted(POLICY_TIERS)})")
    from .coverage import run_ids_for_artifact
    conn = connect()
    artifact = conn.execute("SELECT id FROM qa_artifacts WHERE sha256=?", (artifact_sha256,)).fetchone()
    run_ids = run_ids_for_artifact(artifact["id"]) if artifact else []
    decision = _decide(POLICY_TIERS[tier], run_ids, hil_configured=hil_configured)
    decision["tier"] = tier
    decision["artifact_sha256"] = artifact_sha256
    return decision
