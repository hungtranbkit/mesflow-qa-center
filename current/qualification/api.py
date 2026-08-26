from __future__ import annotations

import json
from pathlib import Path

from flask import Blueprint, jsonify, request

from .coverage import read_snapshot as coverage_read_snapshot
from .coverage import report as coverage_report
from .deployment import DeploymentError, SandboxManager
from .service import QualificationError, QualificationService
from .store import connect

bp = Blueprint("qualification", __name__, url_prefix="/api/qualification")

# Same default the CLI's --evidence-root already uses (qualification/cli.py)
# -- one shared evidence tree, not a second one the API layer invents.
_EVIDENCE_ROOT = Path("reports/qualification")


def _manager() -> SandboxManager:
    return SandboxManager(_EVIDENCE_ROOT)


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
