"""test_deployment: production-policy suite proving the artifact can be
deployed through its OWN real deployment contract -- `docker compose up`
against the exact compose.yml packaged inside the artifact -- not the
simplified `docker run` ArtifactDeployment.deploy() uses for the rest of
qualification (that path is deliberately hand-built for full network/
volume isolation control; it never touches the artifact's own compose.yml
at all, so it cannot prove the installer contract itself works).

Labeled ISOLATED_TEST_EQUIVALENT, not REAL_TEST_TARGET: no dedicated,
network-reachable TEST agent host is wired into QA Center's own
architecture (a separate remote deploy-agent-v2 target_agent exists in this
workspace for a different project's own pipeline, out of scope here per
"Preserve the current architecture -- do not create another qualification
framework"). This suite exercises the real installer/compose contract
faithfully, just on an isolated Docker network on this host rather than a
dedicated remote TEST host -- and says so honestly in its own result.
"""
from __future__ import annotations

import json
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from .deployment import SELF_CONTAINER, ArtifactDeployment, DeploymentError, _run, _sha
from .evidence import EvidenceStore
from .post_deploy_smoke import PostDeploySmokeRunner
from .store import connect, now

DEPLOYMENT_KIND = "ISOLATED_TEST_EQUIVALENT"

# Precise container_name:/network name: lines only, checked against the
# resolved `docker compose config` output before ever running `up` -- a
# bare substring match on "mesflow-app" also matches the harmless
# `image: mesflow-app:<version>` tag and an isolated network's own
# (harmless-within-an-isolated-network) DNS alias, which produced a real
# false-positive refusal the first time this ran by hand. Module-level so
# it stays testable without needing a live Docker daemon.
FORBIDDEN_NAME_PATTERNS = (
    r"container_name:\s*mesflow-postgres\b", r"container_name:\s*mesflow-app\b",
    r"^\s*name:\s*mesflow_network\b", r"^\s*name:\s*mesflow-edge\b",
)


class TestDeploymentRunner:
    def __init__(self, evidence_root: Path):
        self.conn = connect()
        self.evidence = EvidenceStore(evidence_root)
        self.evidence_root = evidence_root

    def _scenario(self, suite_id: str, run_id: str, key: str, fn) -> dict[str, Any]:
        scenario_id = f"scenario-{uuid.uuid4().hex}"
        self.conn.execute("""INSERT INTO qa_scenario_runs(id,suite_run_id,scenario_key,scenario_version,driver,
          status,started_at) VALUES(?,?,?,?,?,'RUNNING',?)""",
          (scenario_id, suite_id, key, "test-deployment-v1", "DEPLOYMENT", now()))
        self.conn.commit()
        status, actual, first_failure = "PASSED", {}, ""
        try:
            actual = fn() or {}
        except Exception as exc:  # noqa: BLE001
            status, first_failure = "FAILED", "assertion"
            actual = {"error_class": type(exc).__name__, "message": str(exc)}
        evidence = self.evidence.write_json(run_id, f"{key.replace('.', '-')}.json",
            {"scenario": key, "status": status, "actual": actual}, kind="TEST_DEPLOYMENT_EVIDENCE",
            suite_run_id=suite_id, scenario_run_id=scenario_id)
        self.conn.execute("UPDATE qa_scenario_runs SET status=?,finished_at=?,first_failing_step=?,actual_json=? WHERE id=?",
                          (status, now(), first_failure, json.dumps(actual, default=str), scenario_id))
        self.conn.execute("INSERT INTO qa_attempts(scenario_run_id,attempt_no,status,fingerprint,started_at,finished_at) "
                          "VALUES(?,1,?,?,?,?)", (scenario_id, status, "", now(), now()))
        self.conn.commit()
        return {"key": key, "status": status, "actual": actual, "evidence": evidence}

    def run(self, run_id: str, artifact_path: Path, *, registered_sha256: str,
            expected_environment_role: str = "production", keep_environment: bool = False) -> dict[str, Any]:
        # "production" is the correct default, not a misnomer for a QA
        # suite: the artifact's own compose.yml hardcodes
        # `MESFLOW_ENV: production` for the mesflow service (see the .env
        # comment below) -- that's what /api/system/ready will genuinely
        # report here regardless of which role this deployment plays. A
        # real TEST vs PRODUCTION distinction is made by DATABASE_URL/
        # domain/secrets, not by this field, in the real deployment
        # contract this suite is deliberately faithful to.
        artifact_path = artifact_path.resolve()
        suite_id = f"suite-{uuid.uuid4().hex}"
        self.conn.execute("""INSERT INTO qa_suite_runs(id,qualification_run_id,suite_key,layer,required,status,
          started_at,command_json) VALUES(?,?,'test_deployment','deployment',1,'RUNNING',?,'[]')""", (suite_id, run_id, now()))
        self.conn.commit()
        results: list[dict[str, Any]] = []
        project = f"mesflow-qual-testdeploy-{uuid.uuid4().hex[:12]}"
        temp = None
        target_url = ""

        def scenario(key, fn):
            result = self._scenario(suite_id, run_id, key, fn)
            results.append(result)
            if result["status"] == "FAILED":
                raise DeploymentError(f"{key}: {result['actual']}")
            return result

        try:
            def phase_artifact_selected_and_sha_preserved():
                actual_sha = _sha(artifact_path)
                if actual_sha != registered_sha256:
                    raise AssertionError(f"artifact sha256 {actual_sha} != registered {registered_sha256}")
                return {"sha256": actual_sha}
            scenario("test_deployment.artifact_selected_and_sha_preserved", phase_artifact_selected_and_sha_preserved)

            manifest_holder: dict[str, Any] = {}
            network_name = f"{project}-net"
            edge_name = f"{project}-edge"
            app_container = f"{project}-app"
            db_container = f"{project}-db"

            def phase_installer_deploy_succeeds():
                nonlocal temp, target_url
                import subprocess
                import tempfile
                import zipfile
                temp = tempfile.TemporaryDirectory(prefix="mesflow-test-deployment-")
                root = Path(temp.name)
                with zipfile.ZipFile(artifact_path) as archive:
                    archive.extractall(root)
                release_root = root / "mesflow-release"
                manifest = json.loads((release_root / "release.json").read_text(encoding="utf-8"))
                manifest_holder["manifest"] = manifest
                image_tar = release_root / str(manifest["bundle"])
                _run(["docker", "load", "-i", str(image_tar)], timeout=300)
                db_password = uuid.uuid4().hex
                env_file = release_root / ".env"
                env_file.write_text(
                    f"POSTGRES_PASSWORD={db_password}\n"
                    f"MESFLOW_IMAGE={manifest['image']}\n"
                    f"MESFLOW_ADMIN_PASSWORD=Admin@123456\n"
                    # NOT WORKSHOP_COOKIE_SECURE here -- that field is
                    # hardcoded in compose.yml (not `${...}`-templated), so
                    # .env can never reach it; see the isolation-override.yml
                    # environment override below instead.
                    f"DATABASE_URL=postgresql://mesflow:{db_password}@{db_container}:5432/mesflow\n"
                    # The artifact's own compose.yml hardcodes
                    # MESFLOW_ENV=production for the mesflow service (not
                    # `${MESFLOW_ENV:-...}`), unlike ArtifactDeployment's
                    # hand-built `docker run` elsewhere in qualification
                    # which explicitly passes test env vars -- this really
                    # is the real installer contract's own real requirement,
                    # not a QA convenience default: a MESFLOW_SECRET_KEY is
                    # mandatory in production mode (config.py enforces it).
                    # Found by hand: the app container crash-looped with
                    # "MESFLOW_SECRET_KEY must be configured for
                    # production" until this was added.
                    f"MESFLOW_SECRET_KEY={uuid.uuid4().hex}\n",
                    encoding="utf-8")
                # SAFETY: the artifact's packaged compose.yml hardcodes
                # container_name: mesflow-postgres / mesflow-app and an
                # EXTERNAL, host-wide network (mesflow_network / mesflow-
                # edge) -- the exact names the REAL production stack on
                # this host already uses. Running it as-is would either
                # collide with (refuse to start next to) or -- Docker
                # Compose has done this before on this exact host, see
                # install.sh's own container-name collision guard --
                # silently adopt/recreate the real containers. An override
                # file replaces every one of those scalars with unique,
                # isolated names BEFORE anything is ever started; the
                # forbidden-name grep below is a second, independent check
                # that refuses to proceed if any of them still resolve into
                # the final merged config for any reason.
                override = root / "isolation-override.yml"
                override.write_text(
                    "services:\n"
                    f"  postgres:\n    container_name: {db_container}\n"
                    f"  mesflow:\n    container_name: {app_container}\n"
                    # Also hardcoded, not `${...}`-templated: WORKSHOP_COOKIE_SECURE: "1".
                    # Correct for a real deployment behind TLS-terminating
                    # nginx; this isolated qualification target has no TLS
                    # termination at all, so a Secure-flagged session
                    # cookie is silently dropped by every HTTP client after
                    # login -- found by hand: login itself reported success,
                    # but every following "authenticated" read got
                    # AUTH_REQUIRED anyway. ArtifactDeployment.deploy()
                    # already disables this the same way for the exact
                    # same reason (plain-HTTP isolated target, no TLS).
                    "    environment:\n      WORKSHOP_COOKIE_SECURE: \"0\"\n"
                    # Compose overrides APPEND list values by default rather
                    # than replacing them -- found by hand: the base
                    # compose.yml's fixed "127.0.0.1:8080:8080" publish
                    # survived alongside whatever this override set,
                    # colliding with host port 8080 (which the real
                    # production mesflow-app almost always already holds).
                    # `!override` forces a real replacement; replacing with
                    # an empty list is simplest and correct here -- this
                    # suite never needs a host-published port at all, it
                    # reaches the app over the isolated network exactly
                    # like every other qualification suite already does.
                    "    ports: !override []\n"
                    "    networks:\n      default: {}\n      edge: {}\n"
                    "networks:\n"
                    f"  default:\n    name: {network_name}\n    external: false\n"
                    f"  edge:\n    name: {edge_name}\n    external: false\n",
                    encoding="utf-8")
                compose_files = ["-f", str(release_root / "compose.yml"), "-f", str(override)]
                resolved = subprocess.run(["docker", "compose", *compose_files, "--project-directory", str(release_root),
                                           "-p", project, "config"], cwd=release_root,
                                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
                resolved_text = resolved.stdout.decode("utf-8", "replace")
                if resolved.returncode != 0:
                    raise AssertionError(f"docker compose config failed to resolve: {resolved_text[-1600:]}")
                import re as _re
                hit = [pattern for pattern in FORBIDDEN_NAME_PATTERNS if _re.search(pattern, resolved_text, _re.MULTILINE)]
                if hit:
                    raise AssertionError(f"REFUSING to deploy: resolved compose config still declares a "
                                         f"production-shaped container_name/network name (pattern(s) {hit}) -- "
                                         f"isolation override did not take effect")
                # This IS the real installer contract: the artifact's own
                # packaged compose.yml, driven with the real `docker
                # compose up` command an operator/deploy script would run --
                # not ArtifactDeployment's hand-built `docker run` used
                # elsewhere in qualification for finer isolation control.
                # Only reached once the resolved config has been proven
                # collision-free above.
                completed = subprocess.run(
                    ["docker", "compose", *compose_files, "--project-directory", str(release_root),
                     "-p", project, "up", "-d", "--no-build"],
                    cwd=release_root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=300)
                if completed.returncode != 0:
                    raise AssertionError(f"docker compose up failed ({completed.returncode}): "
                                         f"{completed.stdout.decode('utf-8', 'replace')[-1600:]}")
                return {"compose_project": project, "image": manifest["image"], "returncode": completed.returncode,
                       "isolation_check": "PASSED", "app_container": app_container, "db_container": db_container}
            scenario("test_deployment.installer_command_succeeds", phase_installer_deploy_succeeds)

            def phase_runtime_becomes_healthy_and_versioned():
                nonlocal target_url
                try:
                    _run(["docker", "network", "connect", network_name, SELF_CONTAINER])
                    target_url = f"http://{app_container}:8080"
                except DeploymentError:
                    port_text = _run(["docker", "port", app_container, "8080/tcp"]).stdout.decode().strip()
                    target_url = f"http://127.0.0.1:{port_text.rsplit(':', 1)[1]}"
                deadline = time.time() + 150
                ready = None
                while time.time() < deadline:
                    try:
                        with urllib.request.urlopen(f"{target_url}/api/system/ready", timeout=3) as response:
                            ready = json.load(response)
                        if ready.get("ok"):
                            break
                    except Exception:
                        time.sleep(1)
                if not ready or not ready.get("ok"):
                    raise AssertionError("deployed-via-installer runtime did not become ready")
                expected_version = str(manifest_holder["manifest"]["version"])
                if str(ready.get("version")) != expected_version:
                    raise AssertionError(f"runtime reports version {ready.get('version')!r}, expected {expected_version!r}")
                if ready.get("environment") != expected_environment_role:
                    raise AssertionError(f"runtime environment role is {ready.get('environment')!r}, expected {expected_environment_role!r}")
                return {"version": ready.get("version"), "environment": ready.get("environment"),
                       "migration_head": ready.get("migration_head")}
            scenario("test_deployment.runtime_healthy_version_and_role_correct", phase_runtime_becomes_healthy_and_versioned)

            def phase_database_initialized():
                # Policy for this suite: a fresh isolated compose deployment
                # always gets a FRESH database (never points at a real
                # persistent volume) -- "initialized" is the applicable
                # branch of "database preserved or initialized according to
                # policy", not "preserved" (this is a fresh install, not an
                # upgrade -- see qualification/upgrade.py for the preserved-
                # data path).
                inspect = _run(["docker", "inspect", db_container, "--format", "{{.State.Status}}"])
                if inspect.stdout.decode().strip() != "running":
                    raise AssertionError(f"postgres container for this deployment is not running: {inspect.stdout.decode()}")
                return {"database_policy": "initialized_fresh", "app_container": app_container, "db_container": db_container}
            scenario("test_deployment.database_initialized_per_policy", phase_database_initialized)

            def phase_critical_smoke():
                result = PostDeploySmokeRunner(self.evidence_root).run(
                    run_id, target_url, suite_key="test_deployment_smoke", allow_disposable_workflow=False)
                if result["status"] != "PASSED":
                    raise AssertionError(f"post-install smoke checks failed: {[r['key'] for r in result['scenarios'] if r['status']=='FAILED']}")
                return {"smoke_suite_id": result["suite_id"], "scenario_count": len(result["scenarios"])}
            scenario("test_deployment.critical_smoke_checks_pass", phase_critical_smoke)

            status = "PASSED"
        except Exception as exc:
            self.evidence.write_json(run_id, "test-deployment-failure.json", {"error": str(exc)}, kind="TEST_DEPLOYMENT_FAILURE")
            status = "FAILED"
        finally:
            if not keep_environment:
                import subprocess
                if temp is not None:
                    release_root = Path(temp.name) / "mesflow-release"
                    override = Path(temp.name) / "isolation-override.yml"
                    subprocess.run(["docker", "network", "disconnect", "-f", network_name, SELF_CONTAINER],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    if override.is_file():
                        subprocess.run(["docker", "compose", "-f", str(release_root / "compose.yml"), "-f", str(override),
                                        "--project-directory", str(release_root), "-p", project, "down", "-v"],
                                       cwd=release_root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    else:
                        # override was never written (failed before that point) --
                        # nothing was ever started, nothing to tear down.
                        pass
                    # DooD identical-path gotcha, found by hand: compose's
                    # relative bind-mount volumes (./runtime/uploads etc.)
                    # get resolved by the HOST daemon against
                    # --project-directory's literal string -- which is a
                    # path inside THIS container's own /tmp, invisible to
                    # this container's PYTHON process but which the host
                    # daemon happily (re)creates as real, often root-owned,
                    # directories on the actual host's /tmp. temp.cleanup()
                    # below only ever sees this container's own filesystem
                    # view and can't reach those; a throwaway root
                    # container reaching the host's real /tmp is the only
                    # thing that can remove them.
                    subprocess.run(["docker", "run", "--rm", "-v", "/tmp:/hosttmp", "alpine",
                                    "rm", "-rf", f"/hosttmp/{Path(temp.name).name}"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
                    temp.cleanup()

        self.conn.execute("UPDATE qa_suite_runs SET status=?,finished_at=? WHERE id=?", (status, now(), suite_id))
        self.conn.commit()
        return {"suite_id": suite_id, "status": status, "scenarios": results, "deployment_kind": DEPLOYMENT_KIND,
               "compose_project": project}
