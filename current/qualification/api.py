from __future__ import annotations

import json
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Any

from flask import Blueprint, jsonify, request

from .coverage import artifact_report as coverage_artifact_report
from .coverage import read_snapshot as coverage_read_snapshot
from .coverage import report as coverage_report
from .deployment import DeploymentError, SandboxManager
from .policy import POLICY_TIERS, evaluate_tier
from .service import QualificationError, QualificationService
from .store import connect, now

bp = Blueprint("qualification", __name__, url_prefix="/api/qualification")

# Same default the CLI's --evidence-root already uses (qualification/cli.py)
# -- one shared evidence tree, not a second one the API layer invents.
_EVIDENCE_ROOT = Path("reports/qualification")


def _manager() -> SandboxManager:
    return SandboxManager(_EVIDENCE_ROOT)


# ---- Web-triggered async job execution (spec section 8) ----
# Reuses the exact pattern agent.py's own release-build launcher already
# established (_run_qa_release_build/_start_qa_release_build): a background
# daemon thread runs a real subprocess, a small JSON file under
# runtime/qualification_jobs/ is the job's persisted status -- no new queue,
# no new run/evidence store. The subprocess IS `python3 -m qualification.cli
# <argv>`, the same command every real verification in this whole project
# already uses; Re-run/Clone just replay (optionally patched) argv a run
# already recorded on itself via start_run(launch_argv=...).
_JOBS_DIR = Path("runtime/qualification_jobs")
_jobs_file_lock = threading.Lock()
_jobs_processes: dict[str, subprocess.Popen] = {}
_jobs_processes_lock = threading.Lock()


def _job_path(job_id: str) -> Path:
    _JOBS_DIR.mkdir(parents=True, exist_ok=True)
    return _JOBS_DIR / f"{job_id}.json"


def _job_read(job_id: str) -> dict[str, Any] | None:
    path = _job_path(job_id)
    with _jobs_file_lock:
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None


def _job_update(job_id: str, **fields: Any) -> dict[str, Any]:
    path = _job_path(job_id)
    with _jobs_file_lock:
        job: dict[str, Any] = {}
        if path.is_file():
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                job = {}
        job.update(fields, id=job_id, updated_at=now())
        path.write_text(json.dumps(job, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return job


def _run_job(job_id: str, argv: list[str]) -> None:
    try:
        _job_update(job_id, status="RUNNING", started_at=now(), argv=argv)
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace")
        with _jobs_processes_lock:
            _jobs_processes[job_id] = proc
        output_lines: list[str] = []
        for line in proc.stdout:  # type: ignore[union-attr]
            output_lines.append(line)
        proc.wait()
        full_output = "".join(output_lines)
        run_id = None
        try:
            parsed = json.loads(full_output)
            run_id = parsed.get("id")
        except Exception:
            pass
        # A concurrent /cancel call may already have written status=CANCELLED
        # (spec section 13/14 bounded cancel demo) before proc.wait() here
        # returns -- an operator-requested cancel is not a "FAILED" job, so
        # never let this thread overwrite that honest terminal status with
        # the exit-code-derived one.
        already_cancelled = (_job_read(job_id) or {}).get("status") == "CANCELLED"
        status = "CANCELLED" if already_cancelled else ("SUCCESS" if proc.returncode == 0 else "FAILED")
        _job_update(job_id, status=status, exit_code=proc.returncode, run_id=run_id,
                   log_tail=output_lines[-150:], finished_at=now())
    except Exception as exc:
        _job_update(job_id, status="FAILED", error=f"{type(exc).__name__}: {exc}", finished_at=now())
    finally:
        with _jobs_processes_lock:
            _jobs_processes.pop(job_id, None)


def _start_job(argv: list[str]) -> dict[str, Any]:
    job_id = f"job-{uuid.uuid4().hex}"
    job = _job_update(job_id, status="QUEUED", argv=argv, created_at=now())
    threading.Thread(target=_run_job, args=(job_id, argv), daemon=True).start()
    return job


def _patch_argv(argv: list[str], overrides: dict[str, str]) -> list[str]:
    """Re-run replays a stored launch_command verbatim; Clone applies a
    small set of recognized --flag overrides (seed/profile/evidence-root/
    kiosk-count/...) on top of it first -- never inventing flags a command
    doesn't accept, since argparse itself will reject anything bogus when
    the job actually runs."""
    patched = list(argv)
    for flag, value in overrides.items():
        flag_name = f"--{flag}"
        found = False
        for i, token in enumerate(patched):
            if token == flag_name and i + 1 < len(patched):
                patched[i + 1] = str(value)
                found = True
                break
        if not found:
            patched += [flag_name, str(value)]
    return patched


@bp.get("/runs")
def runs():
    rows = connect().execute(
        """SELECT q.*,a.filename artifact_filename,a.sha256 artifact_sha256,e.name environment,e.identity environment_identity
           FROM qa_qualification_runs q JOIN qa_artifacts a ON a.id=q.artifact_id
           JOIN qa_environments e ON e.id=q.environment_id ORDER BY q.started_at DESC LIMIT 200"""
    ).fetchall()
    return jsonify(ok=True, runs=[dict(row) for row in rows])


@bp.get("/runs/<run_id>")
def run_detail(run_id: str):
    try:
        return jsonify(ok=True, run=QualificationService().run_manifest(run_id))
    except QualificationError as exc:
        return jsonify(ok=False, error="NOT_FOUND", message=str(exc)), 404


@bp.get("/coverage")
def coverage():
    run_ids = request.args.getlist("run_id")
    return jsonify(ok=True, coverage=coverage_report(run_ids))


@bp.get("/runs/<run_id>/coverage-snapshot")
def coverage_snapshot(run_id: str):
    # Reads the snapshot persisted at the end of the run -- never
    # recomputes against a scratch database, so this still answers
    # correctly long after the run's own isolated Docker resources (and
    # even its evidence files) are gone.
    snapshot = coverage_read_snapshot(run_id)
    if not snapshot:
        return jsonify(ok=False, error="NOT_FOUND", message="no persisted coverage snapshot for this run"), 404
    return jsonify(ok=True, coverage_snapshot=snapshot)


@bp.get("/certifications")
def certifications():
    rows = connect().execute(
        """SELECT c.*,a.filename artifact_filename,a.sha256 artifact_sha256,e.name environment
           FROM qa_certifications c JOIN qa_artifacts a ON a.id=c.artifact_id
           JOIN qa_environments e ON e.id=c.environment_id ORDER BY c.certified_at DESC LIMIT 200"""
    ).fetchall()
    values = []
    for row in rows:
        item = dict(row)
        item["decision"] = json.loads(item.pop("decision_json"))
        values.append(item)
    return jsonify(ok=True, certifications=values)


# ---- Sandbox UI (spec section 10) / Live run observation (spec section 11) ----
# Backed directly by the same SandboxManager every qualification suite
# already uses -- not a second sandbox registry or a screenshot-only view.
# "Open MESFlow"/"Open Kiosk"/"Open Admin" are left to the UI itself (it
# already gets target_url/published_url below and just appends the right
# path); this layer owns the lifecycle actions and live state a human needs
# to observe or act on a running sandbox.

@bp.get("/sandboxes")
def sandboxes():
    status = request.args.get("status") or None
    return jsonify(ok=True, sandboxes=_manager().list_sandboxes(status=status))


@bp.get("/sandboxes/<sandbox_id>")
def sandbox_detail(sandbox_id: str):
    manager = _manager()
    sandbox = manager.get_sandbox(sandbox_id)
    if not sandbox:
        return jsonify(ok=False, error="NOT_FOUND", message=f"unknown sandbox: {sandbox_id}"), 404
    sandbox["health"] = manager.health(sandbox_id)
    return jsonify(ok=True, sandbox=sandbox)


@bp.get("/sandboxes/<sandbox_id>/logs")
def sandbox_logs(sandbox_id: str):
    try:
        return jsonify(ok=True, logs=_manager().sandbox_logs(sandbox_id))
    except DeploymentError as exc:
        return jsonify(ok=False, error="NOT_FOUND", message=str(exc)), 404


@bp.post("/sandboxes/<sandbox_id>/stop")
def sandbox_stop(sandbox_id: str):
    try:
        return jsonify(ok=True, sandbox=_manager().stop(sandbox_id))
    except DeploymentError as exc:
        return jsonify(ok=False, error="SANDBOX_ERROR", message=str(exc)), 404


@bp.post("/sandboxes/<sandbox_id>/start")
def sandbox_start(sandbox_id: str):
    try:
        return jsonify(ok=True, sandbox=_manager().start(sandbox_id))
    except DeploymentError as exc:
        return jsonify(ok=False, error="SANDBOX_ERROR", message=str(exc)), 409


@bp.post("/sandboxes/<sandbox_id>/retain")
def sandbox_retain(sandbox_id: str):
    # "Keep for Debug" -- marks PERSISTENT so it survives whatever
    # default-ephemeral cleanup the run that created it would otherwise do.
    try:
        return jsonify(ok=True, sandbox=_manager().retain(sandbox_id))
    except DeploymentError as exc:
        return jsonify(ok=False, error="SANDBOX_ERROR", message=str(exc)), 404


@bp.post("/sandboxes/<sandbox_id>/destroy")
def sandbox_destroy(sandbox_id: str):
    manager = _manager()
    sandbox = manager.get_sandbox(sandbox_id)
    if not sandbox:
        return jsonify(ok=False, error="NOT_FOUND", message=f"unknown sandbox: {sandbox_id}"), 404
    manager.destroy(sandbox["namespace"])
    return jsonify(ok=True, sandbox=manager.get_sandbox(sandbox_id))


@bp.get("/runs/<run_id>/resource-samples")
def run_resource_samples(run_id: str):
    # Live run observation (spec section 11): CPU/memory/DB-connections/
    # probe-latency samples persisted by the shared ResourceSampler,
    # queryable while a run is still in flight (not just after it finishes).
    rows = connect().execute(
        "SELECT sandbox_id,sampled_at,metrics_json FROM qa_resource_samples WHERE run_id=? ORDER BY sampled_at",
        (run_id,)).fetchall()
    samples = [{"sandbox_id": row["sandbox_id"], "sampled_at": row["sampled_at"],
               "metrics": json.loads(row["metrics_json"])} for row in rows]
    return jsonify(ok=True, run_id=run_id, samples=samples)


# ---- Live observation (spec section 2/3) ----

def _classify_incident(scenario_key: str, suite_key: str) -> str:
    """EXPECTED_FAULT / RECOVERED_FAULT / UNEXPECTED_INCIDENT /
    QUALIFICATION_FAILURE (spec section 3): a deliberate fault-injection
    scenario (recovery/chaos/rollback's own controlled-failure phases)
    failing IS the qualification failing (a real regression), but the
    fault it INJECTS is expected by design -- distinguished here by suite/
    scenario naming already established elsewhere in this codebase
    (recovery.py/chaos.py/upgrade.py's rollback phase), not by inventing a
    new taxonomy those runners don't already imply."""
    injected_fault_suites = {"recovery", "chaos", "rollback", "offline_burst", "scheduler_reliability"}
    if suite_key in injected_fault_suites:
        return "EXPECTED_FAULT" if "recovered" in scenario_key or "restored" in scenario_key else "QUALIFICATION_FAILURE"
    return "QUALIFICATION_FAILURE"


@bp.get("/runs/<run_id>/live")
def run_live(run_id: str):
    """Everything a human needs to watch a real QA run while it executes,
    built entirely from data other modules already persist (ScenarioRunner's
    qa_scenario_runs, ResourceSampler's qa_resource_samples, IntegrityRunner's
    qa_invariant_results, SandboxManager's qa_deployments, and the new
    progress_json a runner can optionally touch) -- no second live-state
    store, no invented fields."""
    conn = connect()
    run_row = conn.execute(
        """SELECT q.*,a.filename artifact_filename,a.sha256 artifact_sha256,e.target_url
           FROM qa_qualification_runs q JOIN qa_artifacts a ON a.id=q.artifact_id
           JOIN qa_environments e ON e.id=q.environment_id WHERE q.id=?""", (run_id,)).fetchone()
    if not run_row:
        return jsonify(ok=False, error="NOT_FOUND", message=f"unknown qualification run: {run_id}"), 404
    run = dict(run_row)
    progress = json.loads(run.pop("progress_json", None) or "{}")
    run.pop("launch_command_json", None)

    running_scenario = conn.execute(
        """SELECT sr.scenario_key,sr.driver,sr.started_at,s.suite_key,s.layer FROM qa_scenario_runs sr
           JOIN qa_suite_runs s ON s.id=sr.suite_run_id
           WHERE s.qualification_run_id=? AND sr.status='RUNNING' ORDER BY sr.started_at DESC LIMIT 1""",
        (run_id,)).fetchone()
    running_suite = conn.execute(
        "SELECT suite_key,layer,started_at FROM qa_suite_runs WHERE qualification_run_id=? AND status='RUNNING' "
        "ORDER BY started_at DESC LIMIT 1", (run_id,)).fetchone()

    sandboxes = [dict(r) for r in conn.execute(
        "SELECT id,namespace,status,health,app_port,target_url FROM qa_deployments WHERE qualification_run_id=?",
        (run_id,)).fetchall()]

    samples = [dict(r) for r in conn.execute(
        "SELECT sandbox_id,sampled_at,metrics_json FROM qa_resource_samples WHERE run_id=? "
        "ORDER BY sampled_at DESC LIMIT 20", (run_id,)).fetchall()]
    samples = [{"sandbox_id": s["sandbox_id"], "sampled_at": s["sampled_at"],
               "metrics": json.loads(s["metrics_json"])} for s in reversed(samples)]
    latest_sample = samples[-1]["metrics"] if samples else {}

    invariants = [dict(r) for r in conn.execute(
        "SELECT invariant_key,status,created_at FROM qa_invariant_results WHERE qualification_run_id=? "
        "ORDER BY created_at DESC LIMIT 20", (run_id,)).fetchall()]
    invariant_violations = sum(1 for i in invariants if i["status"] == "FAILED")

    domain_counts = conn.execute(
        "SELECT COUNT(*) sessions_scenarios FROM qa_scenario_runs sr JOIN qa_suite_runs s ON s.id=sr.suite_run_id "
        "WHERE s.qualification_run_id=? AND sr.scenario_key LIKE 'sessions.%' AND sr.status='PASSED'",
        (run_id,)).fetchone()

    is_terminal = run["status"] not in ("RUNNING",)
    return jsonify(ok=True, run_id=run_id, terminal=is_terminal, identity={
        "run_id": run_id, "run_kind": run.get("run_kind"), "profile": run.get("profile"),
        "artifact_filename": run.get("artifact_filename"), "artifact_sha256": run.get("artifact_sha256"),
        "started_at": run.get("started_at"), "status": run.get("status"),
    }, progress=progress, current_action={
        "phase": progress.get("phase") or (running_suite["suite_key"] if running_suite else None),
        "scenario": (running_scenario["scenario_key"] if running_scenario else progress.get("scenario")),
        "actor": progress.get("actor"), "action": progress.get("action"),
        "simulated_time_seconds": progress.get("simulated_time_seconds"),
    }, resources={"latest": latest_sample, "recent_samples": samples}, domain={
        "passed_session_scenarios": domain_counts["sessions_scenarios"] if domain_counts else 0,
        "integrity_violations": invariant_violations, "recent_invariants": invariants,
    }, sandboxes=sandboxes)


@bp.get("/runs/<run_id>/incidents")
def run_incidents(run_id: str):
    conn = connect()
    scenario_failures = [dict(r) for r in conn.execute(
        """SELECT sr.id,sr.scenario_key,sr.status,sr.finished_at,sr.first_failing_step,s.suite_key
           FROM qa_scenario_runs sr JOIN qa_suite_runs s ON s.id=sr.suite_run_id
           WHERE s.qualification_run_id=? AND sr.status IN ('FAILED','BLOCKED','FLAKY')
           ORDER BY sr.finished_at DESC""", (run_id,)).fetchall()]
    invariant_failures = [dict(r) for r in conn.execute(
        "SELECT id,invariant_key,status,created_at FROM qa_invariant_results WHERE qualification_run_id=? "
        "AND status='FAILED' ORDER BY created_at DESC", (run_id,)).fetchall()]
    incidents = []
    for row in scenario_failures:
        incidents.append({"timestamp": row["finished_at"], "severity": "ERROR" if row["status"] == "FAILED" else "WARNING",
                          "scenario": row["scenario_key"], "suite": row["suite_key"],
                          "summary": f"{row['scenario_key']} {row['status']}" + (f" at {row['first_failing_step']}" if row["first_failing_step"] else ""),
                          "classification": _classify_incident(row["scenario_key"], row["suite_key"]),
                          "evidence_scenario_run_id": row["id"]})
    for row in invariant_failures:
        incidents.append({"timestamp": row["created_at"], "severity": "ERROR", "scenario": row["invariant_key"],
                          "suite": "invariants", "summary": f"domain invariant violated: {row['invariant_key']}",
                          "classification": "QUALIFICATION_FAILURE", "evidence_invariant_result_id": row["id"]})
    incidents.sort(key=lambda i: i["timestamp"] or "", reverse=True)
    return jsonify(ok=True, run_id=run_id, incidents=incidents)


# ---- Run History: Re-run / Clone / Compare (spec section 7) ----

@bp.post("/runs/<run_id>/rerun")
def run_rerun(run_id: str):
    manifest, err_code = _run_manifest_or_404(run_id)
    if err_code:
        return manifest, err_code
    argv = manifest.get("launch_command") or []
    if not argv:
        return jsonify(ok=False, error="NOT_REPLAYABLE",
                       message="this run has no persisted launch_command (it predates spec section 7's re-run support)"), 409
    job = _start_job(argv)
    return jsonify(ok=True, job=job, original_run_id=run_id), 202


@bp.post("/runs/<run_id>/clone")
def run_clone(run_id: str):
    manifest, err_code = _run_manifest_or_404(run_id)
    if err_code:
        return manifest, err_code
    argv = manifest.get("launch_command") or []
    if not argv:
        return jsonify(ok=False, error="NOT_REPLAYABLE",
                       message="this run has no persisted launch_command (it predates spec section 7's clone support)"), 409
    overrides = request.get_json(silent=True) or {}
    patched = _patch_argv(argv, {k: v for k, v in overrides.items() if isinstance(k, str)})
    job = _start_job(patched)
    return jsonify(ok=True, job=job, cloned_from_run_id=run_id, overrides=overrides), 202


def _run_manifest_or_404(run_id: str):
    try:
        return QualificationService().run_manifest(run_id), None
    except QualificationError as exc:
        return jsonify(ok=False, error="NOT_FOUND", message=str(exc)), 404


@bp.get("/jobs/<job_id>")
def job_status(job_id: str):
    job = _job_read(job_id)
    if not job:
        return jsonify(ok=False, error="NOT_FOUND", message=f"unknown job: {job_id}"), 404
    return jsonify(ok=True, job=job)


@bp.post("/jobs/<job_id>/cancel")
def job_cancel(job_id: str):
    with _jobs_processes_lock:
        proc = _jobs_processes.get(job_id)
    if not proc:
        return jsonify(ok=False, error="NOT_CANCELLABLE", message="job is not currently running (already finished, or unknown)"), 409
    proc.terminate()
    job = _job_update(job_id, status="CANCELLED", finished_at=now())
    return jsonify(ok=True, job=job)


@bp.get("/runs/compare")
def runs_compare():
    run_ids = request.args.getlist("run_id")
    if len(run_ids) < 2:
        return jsonify(ok=False, error="NEED_TWO_RUNS", message="pass at least two ?run_id= values to compare"), 400
    service = QualificationService()
    rows = []
    for rid in run_ids:
        try:
            manifest = service.run_manifest(rid)
        except QualificationError:
            rows.append({"run_id": rid, "error": "NOT_FOUND"})
            continue
        samples = [dict(r) for r in connect().execute(
            "SELECT metrics_json FROM qa_resource_samples WHERE run_id=?", (rid,)).fetchall()]
        cpu_values, rss_values, p95_values = [], [], []
        request_count = error_count = 0
        for s in samples:
            m = json.loads(s["metrics_json"])
            probe = m.get("probe") or {}
            if m.get("app_cpu_percent") is not None:
                cpu_values.append(m["app_cpu_percent"])
            if m.get("app_rss_kb") is not None:
                rss_values.append(m["app_rss_kb"])
            if probe.get("latency_ms") is not None:
                p95_values.append(probe["latency_ms"])
                request_count += 1
                if probe.get("error"):
                    error_count += 1
        invariant_violations = connect().execute(
            "SELECT COUNT(*) n FROM qa_invariant_results WHERE qualification_run_id=? AND status='FAILED'",
            (rid,)).fetchone()["n"]
        incidents = connect().execute(
            "SELECT COUNT(*) n FROM qa_scenario_runs sr JOIN qa_suite_runs s ON s.id=sr.suite_run_id "
            "WHERE s.qualification_run_id=? AND sr.status IN ('FAILED','BLOCKED')", (rid,)).fetchone()["n"]
        started = manifest.get("started_at")
        finished = manifest.get("finished_at")
        rows.append({
            "run_id": rid, "run_kind": manifest.get("run_kind"), "profile": manifest.get("profile"),
            "artifact_sha256": manifest.get("artifact_sha256"), "application_version": manifest.get("application_version"),
            "status": manifest.get("status"), "started_at": started, "finished_at": finished,
            "duration_seconds": None,  # computed client-side from ISO timestamps -- avoids a second date-parsing dependency here
            "request_count": request_count or None, "error_count": error_count or None,
            "cpu_percent_avg": round(sum(cpu_values) / len(cpu_values), 2) if cpu_values else None,
            "rss_kb_max": max(rss_values) if rss_values else None,
            "p95_latency_ms": round(sorted(p95_values)[int(len(p95_values) * 0.95)], 2) if p95_values else None,
            "incidents": incidents, "invariant_violations": invariant_violations,
        })
    return jsonify(ok=True, runs=rows)


# ---- Cross-run / artifact-level coverage (spec section 10) ----

@bp.get("/coverage/artifact/<sha256>")
def coverage_artifact(sha256: str):
    return jsonify(ok=True, coverage=coverage_artifact_report(sha256))


# ---- Release policy tiers (spec section 11) ----

@bp.get("/policy/tiers")
def policy_tiers():
    return jsonify(ok=True, tiers={name: policy for name, policy in POLICY_TIERS.items()})


@bp.get("/policy/evaluate-tier")
def policy_evaluate_tier():
    tier = request.args.get("tier", "").upper()
    sha256 = request.args.get("artifact_sha256", "")
    hil_configured = request.args.get("hil_configured", "").lower() in ("1", "true", "yes")
    if not sha256:
        return jsonify(ok=False, error="ARTIFACT_SHA256_REQUIRED"), 400
    try:
        decision = evaluate_tier(tier, sha256, hil_configured=hil_configured)
    except ValueError as exc:
        return jsonify(ok=False, error="UNKNOWN_TIER", message=str(exc)), 400
    return jsonify(ok=True, decision=decision)


# ---- Kiosk Certification (spec: ESP Kiosk / Kiosk Certification phase) ----

@bp.get("/kiosk/testcases")
def kiosk_testcases():
    from .kiosk_registry import REGISTRY_VERSION, list_registry
    return jsonify(ok=True, registry_version=REGISTRY_VERSION, items=list_registry())


@bp.get("/kiosk/profiles")
def kiosk_profiles():
    from .kiosk_registry import list_profiles
    return jsonify(ok=True, profiles=list_profiles())


@bp.post("/kiosk/run")
def kiosk_run():
    """Launches a real `kiosk-certify` job for an already-registered
    artifact (Run Quick/Full/Network/Offline/Stress/HIL Test -- spec
    section 1). Reuses the exact same async job launcher every other
    web-triggered run already uses (_start_job below), never runs a
    qualification job inline in this request."""
    from .kiosk_registry import PROFILES
    body = request.get_json(silent=True) or {}
    artifact_sha256 = str(body.get("artifact_sha256") or "").strip()
    profile = str(body.get("profile") or "").strip().upper()
    if profile not in PROFILES:
        return jsonify(ok=False, error="UNKNOWN_PROFILE", message=f"expected one of {sorted(PROFILES)}"), 400
    row = connect().execute("SELECT source_path FROM qa_artifacts WHERE sha256=?", (artifact_sha256,)).fetchone()
    if not row:
        return jsonify(ok=False, error="ARTIFACT_NOT_FOUND"), 404
    argv = ["python3", "-m", "qualification.cli", "kiosk-certify", "--artifact", row["source_path"], "--profile", profile]
    job = _start_job(argv)
    return jsonify(ok=True, job=job, profile=profile), 202


@bp.get("/kiosk/certification/<sha256>")
def kiosk_certification_route(sha256: str):
    from .kiosk_registry import kiosk_certification
    return jsonify(ok=True, certification=kiosk_certification(sha256))


@bp.get("/runs/<run_id>/kiosk")
def run_kiosk_results(run_id: str):
    """Section 26/27: this run's kiosk testcases grouped by category, with
    enough detail per failure to render the section-27 failure drawer
    without a second request. Built entirely from qa_scenario_runs/
    qa_suite_runs -- no second store."""
    from .kiosk_registry import CATEGORY_LABEL_VI, REGISTRY

    by_scenario_key = {tc.scenario_key: tc for tc in REGISTRY.values()}
    conn = connect()
    rows = [dict(r) for r in conn.execute(
        """SELECT sr.id,sr.scenario_key,sr.status,sr.started_at,sr.finished_at,sr.first_failing_step,
                  s.suite_key,s.status suite_status
           FROM qa_scenario_runs sr JOIN qa_suite_runs s ON s.id=sr.suite_run_id
           WHERE s.qualification_run_id=? AND s.suite_key IN ('kiosk_emulator','esp_hil')
           ORDER BY sr.started_at""", (run_id,)).fetchall()]
    categories: dict[str, dict[str, Any]] = {}
    for row in rows:
        tc = by_scenario_key.get(row["scenario_key"])
        category = tc.category if tc else ("PHYSICAL_HIL" if row["scenario_key"].startswith("esp.hil_ota.") else "OTHER")
        bucket = categories.setdefault(category, {"category": category,
                                                   "category_label": CATEGORY_LABEL_VI.get(category, category),
                                                   "passed": 0, "failed": 0, "blocked": 0, "total": 0, "items": []})
        bucket["total"] += 1
        status = row["status"]
        if status == "PASSED":
            bucket["passed"] += 1
        elif status in ("BLOCKED", "NOT_CONFIGURED"):
            bucket["blocked"] += 1
        else:
            bucket["failed"] += 1
        bucket["items"].append({
            "scenario_run_id": row["id"], "testcase_id": tc.testcase_id if tc else None,
            "name": tc.name if tc else row["scenario_key"], "scenario_key": row["scenario_key"],
            "status": status, "started_at": row["started_at"], "finished_at": row["finished_at"],
            "first_failing_step": row["first_failing_step"],
        })
    return jsonify(ok=True, run_id=run_id, categories=list(categories.values()))
