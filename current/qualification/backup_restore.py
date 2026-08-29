"""First-class BACKUP_RESTORE qualification using MESFlow's pg_dump format."""
from __future__ import annotations

import hashlib
import json
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from .database import DeterministicDatabase
from .deployment import SandboxManager, _run
from .evidence import EvidenceStore
from .integrity_runner import IntegrityRunner
from .post_deploy_smoke import PostDeploySmokeRunner
from .resource_sampler import ResourceSampler
from .scenario_runner import ScenarioRunner
from .store import connect, now

AUTHORITATIVE_TABLES = (
    "employees", "stations", "production_orders", "parts", "operations",
    "work_sessions", "quantity_movements", "production_trace_events",
)


class BackupRestoreRunner:
    def __init__(self, evidence_root: Path):
        self.conn = connect()
        self.evidence_root = evidence_root
        self.evidence = EvidenceStore(evidence_root)
        self.manager = SandboxManager(evidence_root)
        self.integrity = IntegrityRunner(evidence_root)
        self.sampler = ResourceSampler(evidence_root)
        self.runner = ScenarioRunner(evidence_root, scenario_version="backup-restore-v1",
                                     driver="API", evidence_kind="BACKUP_RESTORE_EVIDENCE")

    def _suite(self, run_id: str, key: str, layer: str) -> str:
        suite_id = f"suite-{uuid.uuid4().hex}"
        self.conn.execute("""INSERT INTO qa_suite_runs(id,qualification_run_id,suite_key,layer,required,status,
          started_at,command_json) VALUES(?,?,?,?,1,'RUNNING',?,'[]')""",
          (suite_id, run_id, key, layer, now()))
        self.conn.commit()
        return suite_id

    def run(self, run_id: str, artifact: Path, *, fixture_version: str = "mesflow-fixture-v1",
            keep_environment: bool = False) -> dict[str, Any]:
        source = restore = None
        database = DeterministicDatabase(self.evidence_root)
        db_suite = self._suite(run_id, "backup_restore_database", "database")
        recovery_suite = self._suite(run_id, "backup_restore_recovery", "recovery")
        results: list[dict[str, Any]] = []
        state: dict[str, Any] = {}

        def scenario(suite_id: str, key: str, fn):
            result = self.runner.run(suite_id, run_id, key, fn)
            results.append(result)
            if result["status"] != "PASSED":
                raise AssertionError(f"{key} failed: {result.get('actual')}")
            return result

        try:
            source = self.manager.deploy(run_id, artifact, fixture_version=fixture_version,
                                         namespace_suffix=f"br-src-{uuid.uuid4().hex[:8]}")
            restore = self.manager.deploy(run_id, artifact, fixture_version=fixture_version,
                                          namespace_suffix=f"br-dst-{uuid.uuid4().hex[:8]}")
            if source["id"] == restore["id"] or source["db_container"] == restore["db_container"]:
                raise AssertionError("source and restore sandboxes are not isolated")
            database.seed(run_id, source["db_container"], fixture_version)

            def backup_source():
                proc = subprocess.run([
                    "docker", "exec", source["db_container"], "pg_dump", "-U", "mesflow_qa",
                    "-d", "mesflow_qa", "-Fc", "--no-owner", "--no-privileges",
                ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
                if proc.returncode or not proc.stdout:
                    raise AssertionError(f"pg_dump failed: {proc.stderr.decode('utf-8', 'replace')[-1200:]}")
                state["dump"] = proc.stdout
                state["checksum"] = hashlib.sha256(proc.stdout).hexdigest()
                state["source_counts"] = {
                    table: database.query_json(source["db_container"], f"SELECT count(*) n FROM {table}")[0]["n"]
                    for table in AUTHORITATIVE_TABLES
                }
                return {"backup_sha256": state["checksum"], "backup_bytes": len(proc.stdout),
                        "source_sandbox_id": source["id"], "table_counts": state["source_counts"]}
            scenario(db_suite, "database.migrations_backup.real_supported_backup", backup_source)

            def restore_separate_sandbox():
                _run(["docker", "stop", "--time", "5", restore["app_container"]])
                drop = subprocess.run([
                    "docker", "exec", "-i", restore["db_container"], "dropdb", "-U", "mesflow_qa",
                    "--if-exists", "mesflow_qa"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                create = subprocess.run([
                    "docker", "exec", "-i", restore["db_container"], "createdb", "-U", "mesflow_qa", "mesflow_qa"],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                restored = subprocess.run([
                    "docker", "exec", "-i", restore["db_container"], "pg_restore", "-U", "mesflow_qa",
                    "-d", "mesflow_qa", "--clean", "--if-exists", "--no-owner"],
                    input=state["dump"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180)
                if drop.returncode or create.returncode or restored.returncode:
                    raise AssertionError(f"supported restore failed: {restored.stdout.decode('utf-8', 'replace')[-1600:]}")
                _run(["docker", "start", restore["app_container"]])
                deadline = time.time() + 120
                while time.time() < deadline:
                    health = self.manager.health(restore["id"])
                    if health.get("healthy"):
                        break
                    time.sleep(1)
                else:
                    raise AssertionError("restored app did not become healthy")
                return {"source_sandbox_id": source["id"], "restore_sandbox_id": restore["id"],
                        "separate": source["id"] != restore["id"], "backup_sha256": state["checksum"]}
            scenario(recovery_suite, "database.migrations_backup.restore_to_separate_sandbox", restore_separate_sandbox)

            def compare_and_verify():
                restored_counts = {
                    table: database.query_json(restore["db_container"], f"SELECT count(*) n FROM {table}")[0]["n"]
                    for table in AUTHORITATIVE_TABLES
                }
                if restored_counts != state["source_counts"]:
                    raise AssertionError(f"authoritative table counts differ after restore: {state['source_counts']} != {restored_counts}")
                checks = self.integrity.assert_ok(run_id, restore["db_container"], context="post-restore")
                return {"source": state["source_counts"], "restored": restored_counts,
                        "integrity": {key: value["status"] for key, value in checks.items()}}
            scenario(recovery_suite, "database.migrations_backup.post_restore_comparison_integrity", compare_and_verify)

            smoke = PostDeploySmokeRunner(self.evidence_root).run(run_id, restore["target_url"])
            sample = self.sampler.sample_once(run_id, restore["id"], app_container=restore["app_container"],
                                              db_container=restore["db_container"], target_url=restore["target_url"])
            status = "PASSED" if smoke["status"] == "PASSED" else "FAILED"
            summary = {"status": status, "source_sandbox_id": source["id"], "restore_sandbox_id": restore["id"],
                       "artifact_sha256": source.get("artifact_sha256"), "backup_sha256": state["checksum"],
                       "smoke": smoke["status"], "resource_sample": sample}
            self.evidence.write_json(run_id, "backup-restore-summary.json", summary, kind="BACKUP_RESTORE_EVIDENCE")
        except Exception as exc:
            status = "FAILED"
            summary = {"status": status, "error": str(exc), "source_sandbox_id": source and source.get("id"),
                       "restore_sandbox_id": restore and restore.get("id")}
        finally:
            for suite_id in (db_suite, recovery_suite):
                self.conn.execute("UPDATE qa_suite_runs SET status=?,finished_at=?,summary_json=? WHERE id=?",
                                  (status, now(), json.dumps(summary, default=str), suite_id))
            self.conn.commit()
            if not keep_environment:
                for sandbox in (source, restore):
                    if sandbox:
                        self.manager.destroy(sandbox["namespace"])
        return {"status": status, "scenarios": results, "summary": summary,
                "source_sandbox_id": source and source.get("id"), "restore_sandbox_id": restore and restore.get("id")}
