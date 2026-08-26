from __future__ import annotations

import json

from flask import Blueprint, jsonify, request

from .coverage import read_snapshot as coverage_read_snapshot
from .coverage import report as coverage_report
from .service import QualificationError, QualificationService
from .store import connect

bp = Blueprint("qualification", __name__, url_prefix="/api/qualification")


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
