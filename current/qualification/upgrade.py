"""upgrade: deterministic upgrade qualification (production-policy suite).

known-previous-version artifact -> fresh isolated DB -> real migrations ->
deterministic fixture seed -> STOP that app container (keep the SAME
Postgres, same volume, same data) -> load + start the artifact UNDER
QUALIFICATION on the SAME database -> its own boot-time migrations run
against genuinely pre-existing data -> verify health, migration head,
data preservation, and a critical post-upgrade workflow.

Deliberately reuses ArtifactDeployment's own building blocks (_run, _sha,
inspect_package, SELF_CONTAINER + network-join) rather than a second
implementation of "load an image and run it" -- the only genuinely new
logic here is keeping ONE Postgres alive across two different app
containers, which ArtifactDeployment.deploy() does not do (by design: a
normal qualification run wants a provably fresh DB, this suite's whole
point is the opposite).
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from .database import DeterministicDatabase
from .deployment import SELF_CONTAINER, ArtifactDeployment, DeploymentError, _run, _sha
from .evidence import EvidenceStore
from .integrity_runner import IntegrityRunner
from .live import LiveMESFlowQualification
from .post_deploy_smoke import PostDeploySmokeRunner
from .scenario_runner import ScenarioRunner
from .store import connect, now

ROW_COUNT_TABLES = ["employees", "production_orders", "parts", "operations", "work_sessions", "quantity_movements"]

_DEVNULL = subprocess.DEVNULL


def _container_logs(*names: str) -> dict[str, str]:
    values = {}
    for name in names:
        result = subprocess.run(["docker", "logs", "--tail", "300", name], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        values[name] = result.stdout.decode("utf-8", "replace")
    return values


def _pg_ready(container: str) -> bool:
    return subprocess.run(["docker", "exec", container, "pg_isready", "-U", "mesflow_qa", "-d", "mesflow_qa"],
                          stdout=_DEVNULL, stderr=_DEVNULL).returncode == 0


class UpgradeRunner:
    def __init__(self, evidence_root: Path):
        self.conn = connect()
        self.evidence = EvidenceStore(evidence_root)
        self.evidence_root = evidence_root
        self._runner = ScenarioRunner(evidence_root, scenario_version="upgrade-v1",
                                      driver="UPGRADE", evidence_kind="UPGRADE_EVIDENCE")
        self._integrity = IntegrityRunner(evidence_root)

    def _scenario(self, suite_id: str, run_id: str, key: str, fn) -> dict[str, Any]:
        return self._runner.run(suite_id, run_id, key, fn)

    @staticmethod
    def _resolve_target_url(app: str) -> str:
        """Same DooD networking fix as ArtifactDeployment.deploy(): join the
        run's own network and address the sibling by container name when
        possible (works from inside the packaged QA Center container);
        fall back to the published host port for a bare-host invocation."""
        port_text = _run(["docker", "port", app, "8080/tcp"]).stdout.decode().strip()
        published_url = f"http://127.0.0.1:{port_text.rsplit(':', 1)[1]}"
        return published_url

    @staticmethod
    def _wait_ready(app: str, target_url: str, timeout_seconds: int = 150) -> dict[str, Any]:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            state = _run(["docker", "inspect", app, "--format", "{{.State.Status}}"]).stdout.decode().strip()
            if state == "exited":
                raise DeploymentError(f"{app} exited before becoming ready: {_container_logs(app)[app][-1600:]}")
            try:
                with urllib.request.urlopen(f"{target_url}/api/system/ready", timeout=3) as response:
                    ready = json.load(response)
                if ready.get("ok"):
                    return ready
            except Exception:
                time.sleep(1)
        raise DeploymentError(f"{app} did not become ready within {timeout_seconds}s")

    def run(self, run_id: str, old_artifact_path: Path, new_artifact_path: Path, *,
            fixture_version: str = "mesflow-fixture-v1", keep_environment: bool = False,
            perform_rollback: bool = False) -> dict[str, Any]:
        suite_id = f"suite-{uuid.uuid4().hex}"
        self.conn.execute("""INSERT INTO qa_suite_runs(id,qualification_run_id,suite_key,layer,required,status,
          started_at,command_json) VALUES(?,?,'upgrade','upgrade',1,'RUNNING',?,'[]')""", (suite_id, run_id, now()))
        self.conn.commit()

        suffix = uuid.uuid4().hex[:12]
        namespace = f"mesflow-upgrade-{suffix}"
        network, volume, db = f"{namespace}-net", f"{namespace}-pg", f"{namespace}-db"
        old_app, new_app = f"{namespace}-old-app", f"{namespace}-new-app"
        results: list[dict[str, Any]] = []
        state: dict[str, Any] = {}
        old_manifest: dict[str, Any] = {}
        new_manifest: dict[str, Any] = {}
        joined_network = False

        def scenario(key, fn):
            # Upgrade phases are strictly sequential and dependent (no
            # point seeding a fixture into a database that never deployed)
            # -- a FAILED phase is recorded (both here and in the DB) and
            # then stops the remaining phases immediately, unlike a
            # suite whose scenarios are independent of each other.
            result = self._scenario(suite_id, run_id, key, fn)
            results.append(result)
            if result["status"] == "FAILED":
                raise DeploymentError(f"{key}: {result['actual']}")
            return result

        def target_url_for(app: str) -> str:
            return f"http://{app}:8080" if joined_network else self._resolve_target_url(app)

        try:
            old_manifest, old_tar, old_temp = ArtifactDeployment.inspect_package(old_artifact_path.resolve())
            try:
                _run(["docker", "load", "-i", str(old_tar)], timeout=300)
            finally:
                old_temp.cleanup()
            new_manifest, new_tar, new_temp = ArtifactDeployment.inspect_package(new_artifact_path.resolve())
            try:
                _run(["docker", "load", "-i", str(new_tar)], timeout=300)
            finally:
                new_temp.cleanup()

            def phase_old_deploy():
                nonlocal joined_network
                _run(["docker", "network", "create", "--label", "mesflow.qualification=true", network])
                _run(["docker", "volume", "create", "--label", "mesflow.qualification=true", volume])
                password = uuid.uuid4().hex
                _run(["docker", "run", "-d", "--name", db, "--network", network, "--label", "mesflow.qualification=true",
                      "-e", "POSTGRES_DB=mesflow_qa", "-e", "POSTGRES_USER=mesflow_qa", "-e", f"POSTGRES_PASSWORD={password}",
                      "-v", f"{volume}:/var/lib/postgresql/data", "postgres:17-alpine"])
                deadline = time.time() + 90
                while time.time() < deadline and not _pg_ready(db):
                    time.sleep(1)
                else:
                    if not _pg_ready(db):
                        raise DeploymentError("upgrade suite's PostgreSQL did not become ready")
                database_url = f"postgresql://mesflow_qa:{password}@{db}:5432/mesflow_qa"
                _run(["docker", "run", "-d", "--name", old_app, "--network", network, "--label", "mesflow.qualification=true",
                      "-e", "MESFLOW_ENV=test", "-e", f"DATABASE_URL={database_url}", "-e", f"WORKSHOP_DATABASE_URL={database_url}",
                      "-e", "MESFLOW_TEST_AUTO_LOGIN=0", "-e", "MESFLOW_ADMIN_PASSWORD=Admin@123456", "-e", "WORKSHOP_COOKIE_SECURE=0",
                      str(old_manifest["image"])])
                try:
                    _run(["docker", "network", "connect", network, SELF_CONTAINER])
                    joined_network = True
                except DeploymentError:
                    joined_network = False
                target_url = target_url_for(old_app)
                ready = self._wait_ready(old_app, target_url)
                state["database_url"] = database_url
                return {"version": old_manifest.get("version"), "migration_head": ready.get("migration_head"),
                       "schema_version": ready.get("schema_version"), "ready": ready}
            scenario("upgrade.old_version_deploys_and_becomes_healthy", phase_old_deploy)
            state["old_ready"] = results[-1]["actual"]["ready"]

            def phase_seed_fixture():
                seeded = DeterministicDatabase(self.evidence_root).seed(run_id, db, fixture_version)
                return {"fixture_version": fixture_version, "facts": seeded["facts"]}
            scenario("upgrade.fixture_seeded_on_old_version", phase_seed_fixture)

            database = DeterministicDatabase(self.evidence_root)

            def phase_capture_before():
                counts = {table: database.query_json(db, f"SELECT count(*) n FROM {table}")[0]["n"] for table in ROW_COUNT_TABLES}
                state["before_counts"] = counts
                if perform_rollback:
                    backup = subprocess.run([
                        "docker", "exec", db, "pg_dump", "-U", "mesflow_qa", "-d", "mesflow_qa",
                        "-Fc", "--no-owner", "--no-privileges"],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
                    if backup.returncode or not backup.stdout:
                        raise AssertionError(f"pre-upgrade backup failed: {backup.stderr.decode('utf-8', 'replace')[-1200:]}")
                    state["pre_upgrade_backup"] = backup.stdout
                    state["pre_upgrade_backup_sha256"] = hashlib.sha256(backup.stdout).hexdigest()
                return counts
            scenario("upgrade.before_row_counts_captured", phase_capture_before)

            def phase_swap_app_container():
                # The whole point of an upgrade test: remove ONLY the old
                # app container. db/network/volume -- and every row already
                # inside that database -- stay exactly as they are.
                _run(["docker", "rm", "-f", old_app])
                _run(["docker", "run", "-d", "--name", new_app, "--network", network, "--label", "mesflow.qualification=true",
                      "-e", "MESFLOW_ENV=test", "-e", f"DATABASE_URL={state['database_url']}",
                      "-e", f"WORKSHOP_DATABASE_URL={state['database_url']}", "-e", "MESFLOW_TEST_AUTO_LOGIN=0",
                      "-e", "MESFLOW_ADMIN_PASSWORD=Admin@123456", "-e", "WORKSHOP_COOKIE_SECURE=0",
                      str(new_manifest["image"])])
                target_url = target_url_for(new_app)
                ready = self._wait_ready(new_app, target_url)
                state["new_target_url"] = target_url
                return {"version": new_manifest.get("version"), "migration_head": ready.get("migration_head"),
                       "schema_version": ready.get("schema_version"), "ready": ready}
            scenario("deployment.version_health.new_artifact_migrates_and_becomes_healthy", phase_swap_app_container)
            state["new_ready"] = results[-1]["actual"]["ready"]

            def phase_capture_after_and_verify_preserved():
                counts = {table: database.query_json(db, f"SELECT count(*) n FROM {table}")[0]["n"] for table in ROW_COUNT_TABLES}
                before = state["before_counts"]
                lost = {t: (before[t], counts[t]) for t in ROW_COUNT_TABLES if counts[t] < before[t]}
                if lost:
                    raise AssertionError(f"row counts DECREASED after upgrade (data loss): {lost}")
                return {"before": before, "after": counts}
            scenario("upgrade.historical_data_preserved", phase_capture_after_and_verify_preserved)

            def phase_migration_head_valid():
                old_head, new_head = state["old_ready"].get("migration_head"), state["new_ready"].get("migration_head")
                if not new_head:
                    raise AssertionError("post-upgrade /api/system/ready has no migration_head")
                return {"old_migration_head": old_head, "new_migration_head": new_head, "changed": old_head != new_head}
            scenario("upgrade.migration_head_valid", phase_migration_head_valid)

            def phase_integrity_after_migration():
                checks = self._integrity.assert_ok(run_id, db, context="post-migration")
                return {"checks": {key: value["status"] for key, value in checks.items()}}
            scenario("database.migrations_backup.integrity_after_migration", phase_integrity_after_migration)

            def phase_critical_workflow_after_upgrade():
                live = LiveMESFlowQualification(run_id, state["new_target_url"], db, self.evidence_root, app_container=new_app)
                live.login()
                workflow_results = live.workflows(suite_key="upgrade_post_workflow_smoke")
                failed = [r for r in workflow_results if r["status"] != "PASSED"]
                if failed:
                    raise AssertionError(f"critical workflow(s) failed post-upgrade: {[r['key'] for r in failed]}")
                return {"scenarios_passed": [r["key"] for r in workflow_results]}
            scenario("upgrade.critical_workflows_operate_post_upgrade", phase_critical_workflow_after_upgrade)

            def phase_deployment_version_health_credit():
                # deployment.version_health requires the 'upgrade' layer;
                # this suite already IS that layer -- an explicit
                # feature-keyed scenario (spec section 9's "explicit
                # supported mapping mechanism") so real, already-verified
                # evidence above (post-upgrade health + migration head)
                # counts toward it, not a second re-check of the same facts.
                return {"new_version": new_manifest.get("version"), "new_migration_head": state["new_ready"].get("migration_head"),
                        "post_upgrade_health": state["new_ready"].get("ok")}
            scenario("deployment.version_health.upgrade_verified", phase_deployment_version_health_credit)

            def phase_database_migrations_backup_credit():
                # database.migrations_backup requires the 'migration' layer
                # specifically (its 'database'/'recovery' layers come from
                # backup_restore.py) -- same explicit-mapping rationale.
                old_head = state["old_ready"].get("migration_head")
                new_head = state["new_ready"].get("migration_head")
                return {"old_migration_head": old_head, "new_migration_head": new_head,
                        "real_migration_executed": old_head != new_head}
            scenario("database.migrations_backup.migration_verified", phase_database_migrations_backup_credit)

            if perform_rollback:
                rollback_suite_id = f"suite-{uuid.uuid4().hex}"
                self.conn.execute("""INSERT INTO qa_suite_runs(id,qualification_run_id,suite_key,layer,required,status,
                  started_at,command_json) VALUES(?,?,'rollback','recovery',1,'RUNNING',?,'[]')""", (rollback_suite_id, run_id, now()))
                self.conn.commit()

                def rollback_scenario(key, fn):
                    result = self._scenario(rollback_suite_id, run_id, key, fn)
                    results.append(result)
                    if result["status"] == "FAILED":
                        raise DeploymentError(f"{key}: {result['actual']}")
                    return result

                def phase_inject_controlled_failure():
                    # A real, controlled post-upgrade failure -- the actual
                    # trigger scripts/deploy.sh's own automatic rollback
                    # responds to (a deploy that becomes unhealthy). Stopping
                    # the new app container abruptly is the safest reliable
                    # way to reproduce "this deploy is unhealthy" against a
                    # disposable QA sandbox.
                    _run(["docker", "stop", "--time", "3", new_app])
                    return {"new_app_stopped": True}

                def phase_execute_real_rollback():
                    # The repository's supported production strategy is
                    # scripts/backup.sh + scripts/restore.sh: restore the
                    # pre-upgrade custom-format pg_dump, then boot the old
                    # app. deploy.sh does not promise automatic reverse
                    # migrations, so do not manufacture that capability.
                    subprocess.run(["docker", "rm", "-f", new_app], stdout=_DEVNULL, stderr=_DEVNULL)
                    drop = subprocess.run(["docker", "exec", db, "dropdb", "-U", "mesflow_qa", "--if-exists", "mesflow_qa"],
                                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                    create = subprocess.run(["docker", "exec", db, "createdb", "-U", "mesflow_qa", "mesflow_qa"],
                                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                    restore = subprocess.run([
                        "docker", "exec", "-i", db, "pg_restore", "-U", "mesflow_qa", "-d", "mesflow_qa",
                        "--clean", "--if-exists", "--no-owner"], input=state["pre_upgrade_backup"],
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180)
                    if drop.returncode or create.returncode or restore.returncode:
                        raise AssertionError(f"pre-upgrade backup restore failed: {restore.stdout.decode('utf-8', 'replace')[-1600:]}")
                    _run(["docker", "run", "-d", "--name", old_app, "--network", network, "--label", "mesflow.qualification=true",
                          "-e", "MESFLOW_ENV=test", "-e", f"DATABASE_URL={state['database_url']}",
                          "-e", f"WORKSHOP_DATABASE_URL={state['database_url']}", "-e", "MESFLOW_TEST_AUTO_LOGIN=0",
                          "-e", "MESFLOW_ADMIN_PASSWORD=Admin@123456", "-e", "WORKSHOP_COOKIE_SECURE=0",
                          str(old_manifest["image"])])
                    rollback_target_url = target_url_for(old_app)
                    ready = self._wait_ready(old_app, rollback_target_url, timeout_seconds=120)
                    state["rollback_target_url"] = rollback_target_url
                    return {"strategy": "pre-upgrade-db-restore", "backup_sha256": state["pre_upgrade_backup_sha256"],
                            "reverted_to_version": old_manifest.get("version"),
                            "post_rollback_migration_head": ready.get("migration_head"), "post_rollback_ready": ready.get("ok")}

                def phase_verify_rollback_data_and_integrity():
                    counts = {table: database.query_json(db, f"SELECT count(*) n FROM {table}")[0]["n"] for table in ROW_COUNT_TABLES}
                    before = state["before_counts"]
                    lost = {t: (before[t], counts[t]) for t in ROW_COUNT_TABLES if counts[t] < before[t]}
                    if lost:
                        raise AssertionError(f"row counts DECREASED after rollback (data loss): {lost}")
                    integrity_runner = IntegrityRunner(self.evidence_root)
                    checks = integrity_runner.assert_ok(run_id, db, context="post-rollback")
                    return {"row_counts_after_rollback": counts, "integrity": {k: v["status"] for k, v in checks.items()}}

                def phase_critical_smoke_after_rollback():
                    smoke = PostDeploySmokeRunner(self.evidence_root).run(
                        run_id, state["rollback_target_url"], allow_disposable_workflow=False, suite_key="post_rollback_smoke")
                    if smoke["status"] != "PASSED":
                        raise AssertionError(f"post-rollback smoke did not pass: {smoke['status']}")
                    return {"smoke_status": smoke["status"]}

                try:
                    rollback_scenario("deployment.version_health.controlled_post_upgrade_failure_injected", phase_inject_controlled_failure)
                    rollback_scenario("deployment.version_health.real_rollback_executed", phase_execute_real_rollback)
                    rollback_scenario("deployment.version_health.rollback_data_preserved_and_consistent", phase_verify_rollback_data_and_integrity)
                    rollback_scenario("deployment.version_health.critical_smoke_after_rollback", phase_critical_smoke_after_rollback)
                    rollback_final_status = "PASSED"
                except Exception:
                    rollback_final_status = "FAILED"
                    raise
                finally:
                    self.conn.execute("UPDATE qa_suite_runs SET status=?,finished_at=? WHERE id=?",
                                      (rollback_final_status, now(), rollback_suite_id))
                    self.conn.commit()

            status = "PASSED"
        except Exception as exc:
            self.evidence.write_json(run_id, "upgrade-suite-failure.json",
                {"error": str(exc), "logs": _container_logs(old_app, new_app, db)}, kind="UPGRADE_SUITE_FAILURE")
            status = "FAILED"
        finally:
            if not keep_environment:
                for name in (old_app, new_app, db):
                    subprocess.run(["docker", "rm", "-f", name], stdout=_DEVNULL, stderr=_DEVNULL)
                if joined_network:
                    subprocess.run(["docker", "network", "disconnect", "-f", network, SELF_CONTAINER], stdout=_DEVNULL, stderr=_DEVNULL)
                subprocess.run(["docker", "network", "rm", network], stdout=_DEVNULL, stderr=_DEVNULL)
                subprocess.run(["docker", "volume", "rm", volume], stdout=_DEVNULL, stderr=_DEVNULL)

        self.conn.execute("UPDATE qa_suite_runs SET status=?,finished_at=? WHERE id=?", (status, now(), suite_id))
        self.conn.commit()
        return {"suite_id": suite_id, "status": status, "scenarios": results,
               "old_version": old_manifest.get("version"), "new_version": new_manifest.get("version"),
               "old_sha256": _sha(old_artifact_path.resolve()), "new_sha256": _sha(new_artifact_path.resolve()),
               "namespace": namespace}
