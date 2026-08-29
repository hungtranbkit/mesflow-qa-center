from __future__ import annotations

import hashlib
import json
import socket
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .store import connect, now


class QualificationError(RuntimeError):
    pass


# Run Manager consolidation (spec section 4): qa_qualification_runs is the
# one shared run-history table for every kind of QA run, not just release
# qualification -- RELEASE_QUALIFICATION remains the high-confidence
# certification use case, but the same row shape/suite/scenario/evidence
# infrastructure now also represents these. Not every kind has a wired
# caller yet (LONG_RUNNING_FACTORY_SIMULATION/OFFLINE_BURST/CHAOS/
# MIGRATION/BACKUP_RESTORE/ROLLBACK are schema-ready but not yet produced
# by any real command -- honestly reported, not faked).
RUN_KINDS = {
    "FUNCTIONAL", "UI_E2E", "LONG_RUNNING_FACTORY_SIMULATION", "LOAD", "SOAK",
    "OFFLINE_BURST", "CHAOS", "MIGRATION", "BACKUP_RESTORE", "ROLLBACK",
    "RELEASE_QUALIFICATION",
}


class QualificationService:
    def __init__(self):
        self.conn = connect()

    @staticmethod
    def _id(prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex}"

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def register_artifact(self, *, application_version: str, git_commit: str, path: Path,
                          media_type: str = "application/octet-stream") -> dict[str, Any]:
        path = path.resolve(strict=True)
        if not path.is_file():
            raise QualificationError("artifact must be a regular file")
        sha256 = self._sha256(path)
        existing = self.conn.execute("SELECT * FROM qa_artifacts WHERE sha256=?", (sha256,)).fetchone()
        if existing:
            return dict(existing)
        release_id = self._id("rel")
        artifact_id = self._id("art")
        timestamp = now()
        self.conn.execute("INSERT INTO qa_releases(id,application_version,git_commit,created_at) VALUES(?,?,?,?)",
                          (release_id, application_version, git_commit, timestamp))
        self.conn.execute(
            """INSERT INTO qa_artifacts(id,release_id,filename,sha256,size_bytes,media_type,source_path,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (artifact_id, release_id, path.name, sha256, path.stat().st_size, media_type, str(path), timestamp),
        )
        self.conn.commit()
        return dict(self.conn.execute("SELECT * FROM qa_artifacts WHERE id=?", (artifact_id,)).fetchone())

    def attest_environment(self, *, name: str, kind: str, target_url: str,
                           database_identity: str, destructive_allowed: bool = False,
                           identity: str | None = None) -> dict[str, Any]:
        kind = kind.upper()
        if kind not in {"LOCAL", "QA", "TEST", "PRODUCTION_TEST", "PRODUCTION"}:
            raise QualificationError("invalid environment kind")
        parsed = urlparse(target_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise QualificationError("target_url must be an absolute HTTP(S) URL")
        if destructive_allowed and kind not in {"QA", "TEST"}:
            raise QualificationError("destructive qualification is allowed only for QA/TEST environments")
        environment_id = self._id("env")
        attestation = identity or f"{kind}:{parsed.hostname}:{database_identity}"
        self.conn.execute(
            """INSERT INTO qa_environments(id,name,identity,kind,target_url,hostname,database_identity,
               destructive_allowed,attested_at) VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(name) DO UPDATE SET identity=excluded.identity,kind=excluded.kind,target_url=excluded.target_url,
               hostname=excluded.hostname,database_identity=excluded.database_identity,
               destructive_allowed=excluded.destructive_allowed,attested_at=excluded.attested_at""",
            (environment_id, name, attestation, kind, target_url.rstrip("/"), parsed.hostname,
             database_identity, int(destructive_allowed), now()),
        )
        self.conn.commit()
        return dict(self.conn.execute("SELECT * FROM qa_environments WHERE name=?", (name,)).fetchone())

    def start_run(self, *, artifact_id: str, environment_id: str, profile: str,
                  dataset_version: str, scenario_set_version: str,
                  run_kind: str = "RELEASE_QUALIFICATION", launch_argv: list[str] | None = None,
                  cloned_from_run_id: str | None = None) -> dict[str, Any]:
        if not self.conn.execute("SELECT 1 FROM qa_artifacts WHERE id=?", (artifact_id,)).fetchone():
            raise QualificationError("unknown artifact")
        env = self.conn.execute("SELECT * FROM qa_environments WHERE id=?", (environment_id,)).fetchone()
        if not env:
            raise QualificationError("unknown environment")
        if run_kind not in RUN_KINDS:
            raise QualificationError(f"invalid run_kind: {run_kind!r} (must be one of {sorted(RUN_KINDS)})")
        active = self.conn.execute(
            "SELECT id FROM qa_qualification_runs WHERE artifact_id=? AND environment_id=? AND status='RUNNING'",
            (artifact_id, environment_id),
        ).fetchone()
        if active:
            raise QualificationError(f"qualification already running: {active['id']}")
        run_id = self._id("run")
        self.conn.execute(
            """INSERT INTO qa_qualification_runs(id,artifact_id,environment_id,profile,dataset_version,
               scenario_set_version,status,started_at,run_kind,launch_command_json,cloned_from_run_id)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, artifact_id, environment_id, profile, dataset_version, scenario_set_version, "RUNNING", now(),
             run_kind, json.dumps(launch_argv or []), cloned_from_run_id),
        )
        self.conn.commit()
        return dict(self.conn.execute("SELECT * FROM qa_qualification_runs WHERE id=?", (run_id,)).fetchone())

    def touch_progress(self, run_id: str, **fields: Any) -> dict[str, Any]:
        """Live observation (spec section 2/6): merge real, runner-reported
        progress fields (phase/scenario_key/actor/action/simulated_time_seconds/
        anything else a specific runner genuinely has) into one JSON blob,
        never inventing a field a caller didn't actually pass. Safe to call
        often -- it's a single small UPDATE, not a new high-frequency store."""
        row = self.conn.execute("SELECT progress_json FROM qa_qualification_runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise QualificationError(f"unknown qualification run: {run_id}")
        progress = json.loads(row["progress_json"] or "{}")
        progress.update(fields)
        progress["updated_at"] = now()
        self.conn.execute("UPDATE qa_qualification_runs SET progress_json=? WHERE id=?",
                          (json.dumps(progress, default=str), run_id))
        self.conn.commit()
        return progress

    def finish_run(self, run_id: str) -> dict[str, Any]:
        suites = [dict(r) for r in self.conn.execute(
            "SELECT * FROM qa_suite_runs WHERE qualification_run_id=? ORDER BY started_at", (run_id,)).fetchall()]
        if not suites:
            status = "BLOCKED"
        elif any(s["required"] and s["status"] in {"FAILED", "BLOCKED"} for s in suites):
            status = "FAILED"
        elif any(s["status"] == "FLAKY" for s in suites):
            status = "FLAKY"
        elif all(s["status"] == "PASSED" for s in suites):
            status = "PASSED"
        else:
            status = "BLOCKED"
        invariant_failures = self.conn.execute(
            "SELECT COUNT(*) n FROM qa_invariant_results WHERE qualification_run_id=? AND status='FAILED'", (run_id,)
        ).fetchone()["n"]
        if invariant_failures:
            status = "FAILED"
        fingerprints = [r["fingerprint"] for r in self.conn.execute(
            "SELECT DISTINCT fingerprint FROM qa_scenario_runs WHERE suite_run_id IN "
            "(SELECT id FROM qa_suite_runs WHERE qualification_run_id=?) AND fingerprint<>''", (run_id,)).fetchall()]
        summary = {"suite_count": len(suites), "invariant_failures": invariant_failures,
                   "failed_fingerprints": fingerprints}
        self.conn.execute(
            "UPDATE qa_qualification_runs SET status=?,finished_at=?,failed_fingerprints=?,summary_json=? WHERE id=?",
            (status, now(), json.dumps(fingerprints), json.dumps(summary, sort_keys=True), run_id),
        )
        self.conn.commit()
        return self.run_manifest(run_id)

    def mark_cancelled(self, run_id: str) -> dict[str, Any]:
        """Spec section 13/14: an operator-cancelled long-simulation job (web
        job launcher's /jobs/<id>/cancel -> SIGTERM) is NOT "completed" --
        finish_run()'s PASSED/FAILED/BLOCKED/FLAKY derivation from suite rows
        would be dishonest here (the suite never got to run to a real
        conclusion). CANCELLED is a distinct, honest terminal status so a
        QA Center restart (or the Run History list) never mistakes a
        deliberately-stopped run for a completed qualification result."""
        self.conn.execute(
            "UPDATE qa_qualification_runs SET status='CANCELLED',finished_at=? WHERE id=? AND status='RUNNING'",
            (now(), run_id),
        )
        self.conn.commit()
        return self.run_manifest(run_id)

    def run_manifest(self, run_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            """SELECT q.*,a.filename artifact_filename,a.sha256 artifact_sha256,a.size_bytes artifact_size_bytes,
               r.application_version,r.git_commit,e.name environment,e.kind environment_kind,e.identity environment_identity,
               e.target_url,e.hostname,e.database_identity
               FROM qa_qualification_runs q JOIN qa_artifacts a ON a.id=q.artifact_id
               JOIN qa_releases r ON r.id=a.release_id JOIN qa_environments e ON e.id=q.environment_id WHERE q.id=?""",
            (run_id,),
        ).fetchone()
        if not row:
            raise QualificationError("unknown qualification run")
        result = dict(row)
        for key in ("failed_fingerprints", "summary_json"):
            result[key.removesuffix("_json")] = json.loads(result.pop(key) or ("[]" if key == "failed_fingerprints" else "{}"))
        result["launch_command"] = json.loads(result.pop("launch_command_json", None) or "[]")
        result["progress"] = json.loads(result.pop("progress_json", None) or "{}")
        result["suites"] = [dict(r) for r in self.conn.execute(
            "SELECT * FROM qa_suite_runs WHERE qualification_run_id=? ORDER BY started_at", (run_id,)).fetchall()]
        result["evidence"] = [dict(r) for r in self.conn.execute(
            "SELECT * FROM qa_evidence WHERE qualification_run_id=? ORDER BY created_at", (run_id,)).fetchall()]
        result["production_eligible"] = False
        return result
