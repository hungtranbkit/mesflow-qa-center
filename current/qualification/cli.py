from __future__ import annotations

import argparse
import json
import os
import subprocess
import uuid
from pathlib import Path

from .policy import evaluate
from .build_integrity import BuildIntegrityRunner
from .coverage import snapshot as coverage_snapshot
from .critical_unit import run as run_critical_unit
from .database import DeterministicDatabase
from .deployment import ArtifactDeployment
from .esp_hil import EspHilRunner
from .kiosk_emulator import KioskEmulatorRunner
from .live import LiveMESFlowQualification
from .load_soak import LoadSoakRunner
from .post_deploy_smoke import PostDeploySmokeRunner
from .recovery import RecoveryRunner
from .replay import replay_scenario as replay_scenario_fn
from .runner import QualificationRunner
from .service import QualificationService
from .store import connect, now
from .test_deployment import TestDeploymentRunner
from .upgrade import UpgradeRunner


SUITE_RUNNERS = {"api_contract", "integration", "mes_workflows", "invariants", "ui_critical"}


def _json(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str))


def _git_commit(root: Path) -> str:
    return subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], check=True,
                          capture_output=True, text=True).stdout.strip()


def _safe_inspect_package(artifact_path: Path) -> tuple[dict | None, int | None]:
    """Every command that qualifies a real artifact starts by reading its
    manifest. Found by hand: a sufficiently corrupted zip (a bad CRC-32,
    not just a checksums.txt mismatch build_integrity's own dedicated
    scenario catches) raised straight out of zipfile.extractall() and
    crashed the whole CLI with a bare Python traceback -- no JSON, no
    qualification run record, nothing evidenced or blocked. This is the
    one place that first contact with an artifact's bytes happens, so it's
    the one place that must never let a corrupted/malformed artifact
    escape as an unhandled exception -- it must become a real, structured,
    machine-readable BLOCKED result instead."""
    try:
        manifest, _, temp = ArtifactDeployment.inspect_package(artifact_path.resolve())
        temp.cleanup()
        return manifest, None
    except Exception as exc:
        _json({"status": "BLOCKED", "blocked_at": "artifact_inspection",
              "error": str(exc), "error_class": type(exc).__name__,
              "message": "the artifact could not be opened/extracted -- corrupted, truncated, or not a real "
                        "MESFlow release bundle; no qualification run was created because no version identity "
                        "could be read from it"})
        return None, 1


def _run_browser_suite_or_record_failure(run_id: str, target_url: str, evidence_root: Path) -> dict:
    """Run the real Playwright critical-UI suite as part of the one-command
    real-artifact flow (spec section 7). Import is deferred so that a host
    without Playwright installed can still run --skip-browser; but if the
    suite is requested and something prevents it from actually executing
    (missing browsers, a crash before the first scenario, etc.), that must
    surface as a FAILED required suite -- never as a silently-missing one,
    and never as a false PASS. Per-scenario failures inside a suite that
    did run are already handled by run_browser_suite() itself.
    """
    try:
        from .browser import run_browser_suite
        return run_browser_suite(run_id, target_url, evidence_root)
    except Exception as exc:
        suite_id = f"suite-{uuid.uuid4().hex}"
        conn = connect()
        started = now()
        conn.execute("""INSERT INTO qa_suite_runs(id,qualification_run_id,suite_key,layer,required,status,
          started_at,finished_at,command_json,summary_json) VALUES(?,?,'ui_critical','browser',1,'FAILED',?,?,'[]',?)""",
          (suite_id, run_id, started, now(), json.dumps({"error": str(exc), "error_class": type(exc).__name__})))
        conn.commit()
        return {"suite_id": suite_id, "status": "FAILED", "scenarios": [],
                "error": str(exc), "error_class": type(exc).__name__}


def _run_suite_or_record_failure(run_id: str, suite_key: str, layer: str, fn) -> dict:
    """Same failure-honesty contract as _run_browser_suite_or_record_failure,
    generalized: if one of the newer production-policy suite runners raises
    before it can record its own FAILED scenario (an unexpected bug in the
    suite runner itself, a Docker daemon hiccup, etc.), that must still
    surface as a FAILED required suite for THIS run, never as a silently
    missing one."""
    try:
        return fn()
    except Exception as exc:
        suite_id = f"suite-{uuid.uuid4().hex}"
        conn = connect()
        started = now()
        conn.execute("""INSERT INTO qa_suite_runs(id,qualification_run_id,suite_key,layer,required,status,
          started_at,finished_at,command_json,summary_json) VALUES(?,?,?,?,1,'FAILED',?,?,'[]',?)""",
          (suite_id, run_id, suite_key, layer, started, now(), json.dumps({"error": str(exc), "error_class": type(exc).__name__})))
        conn.commit()
        return {"suite_id": suite_id, "status": "FAILED", "scenarios": [],
                "error": str(exc), "error_class": type(exc).__name__}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mesflow-qualify")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="run a headless qualification profile")
    run.add_argument("--artifact", required=True, type=Path)
    run.add_argument("--version", required=True)
    run.add_argument("--source-root", required=True, type=Path)
    run.add_argument("--environment-name", required=True)
    run.add_argument("--environment-kind", choices=["LOCAL", "QA", "TEST", "PRODUCTION_TEST"], required=True)
    run.add_argument("--target-url", required=True)
    run.add_argument("--database-identity", required=True)
    run.add_argument("--dataset-version", required=True)
    run.add_argument("--scenario-set-version", required=True)
    run.add_argument("--profile", choices=["quick", "full"], required=True)
    run.add_argument("--suite-config", required=True, type=Path)
    run.add_argument("--evidence-root", type=Path, default=Path("reports/qualification"))

    artifact_run = commands.add_parser("run-artifact", help="deploy and qualify an immutable MESFlow image artifact "
                                                            "against every implemented production-policy suite")
    artifact_run.add_argument("--artifact", required=True, type=Path)
    artifact_run.add_argument("--fixture-version", default="mesflow-fixture-v1")
    artifact_run.add_argument("--scenario-set-version", default="mesflow-software-v1")
    artifact_run.add_argument("--evidence-root", type=Path, default=Path("reports/qualification"))
    artifact_run.add_argument("--keep-environment", action="store_true")
    artifact_run.add_argument("--source-root", type=Path, default=None,
                              help="MESFlow app repo root, for critical_unit; defaults to $MESFLOW_SOURCE_ROOT")
    artifact_run.add_argument("--old-artifact", type=Path, default=None,
                              help="a known-previously-qualified artifact; supplying this enables the upgrade suite "
                                   "(omitted by default -- upgrade needs a real predecessor, never a fresh DB)")
    artifact_run.add_argument("--skip-browser", action="store_true",
                              help="omit the Playwright critical-UI suite (e.g. no browsers installed on this host); "
                                   "the run is then honestly incomplete for certification, not falsely full")
    artifact_run.add_argument("--skip-build-integrity", action="store_true")
    artifact_run.add_argument("--skip-critical-unit", action="store_true")
    artifact_run.add_argument("--skip-kiosk-emulator", action="store_true")
    artifact_run.add_argument("--skip-recovery", action="store_true")
    artifact_run.add_argument("--skip-test-deployment", action="store_true")
    artifact_run.add_argument("--skip-post-deploy-smoke", action="store_true")
    artifact_run.add_argument("--skip-load-soak", action="store_true")
    artifact_run.add_argument("--soak-window-seconds", type=float, default=60.0)
    artifact_run.add_argument("--enable-hil", action="store_true",
                              help="attempt the esp_hil suite (real hardware); NOT_CONFIGURED/BLOCKED honestly "
                                   "when no device or no reachable debug path, never silently skipped")
    artifact_run.add_argument("--hil-backend-url", default=None,
                              help="tunnel URL the physical device can reach back to THIS deployment; required "
                                   "for esp_hil to progress past detection")
    # Whether esp_hil PASS is actually MANDATORY for production_eligible is
    # a certify-time policy input (--hil-configured on the `certify`
    # command below), not something this command decides -- run-artifact
    # always attempts/records esp_hil honestly regardless (NOT_CONFIGURED
    # unless --enable-hil is passed); certify is what enforces it.
    artifact_run.add_argument("--intentional-wrong-quantity", action="store_true", help=argparse.SUPPRESS)

    replay = commands.add_parser("replay-suite", help="replay one suite against a fresh isolated deployment of the "
                                                        "same artifact (spec section 8): resets to a correct "
                                                        "deterministic fixture first, never reuses stale state")
    replay.add_argument("--artifact", required=True, type=Path)
    replay.add_argument("--suite", required=True, choices=sorted(SUITE_RUNNERS))
    replay.add_argument("--fixture-version", default="mesflow-fixture-v1")
    replay.add_argument("--evidence-root", type=Path, default=Path("reports/qualification"))
    replay.add_argument("--keep-environment", action="store_true")
    replay.add_argument("--replays-run-id", default="", help="the original qualification run this is replaying, "
                                                              "for traceability only (not schema-enforced)")

    replay_scenario_cmd = commands.add_parser("replay-scenario", help="replay exactly one scenario from a prior run "
                                                                       "(spec section 9): same artifact sha256, same "
                                                                       "fixture version, fresh isolated environment")
    replay_scenario_cmd.add_argument("--scenario-run-id", required=True)
    replay_scenario_cmd.add_argument("--evidence-root", type=Path, default=Path("reports/qualification"))
    replay_scenario_cmd.add_argument("--keep-environment", action="store_true")

    hil_cmd = commands.add_parser("hil", help="run only the esp_hil suite (spec section 12/21), against a fresh "
                                              "isolated deployment of one artifact")
    hil_cmd.add_argument("--artifact", required=True, type=Path)
    hil_cmd.add_argument("--fixture-version", default="mesflow-fixture-v1")
    hil_cmd.add_argument("--evidence-root", type=Path, default=Path("reports/qualification"))
    hil_cmd.add_argument("--keep-environment", action="store_true")
    hil_cmd.add_argument("--backend-url", default=None)

    soak_cmd = commands.add_parser("soak", help="run only the load_soak suite (spec section 13/21), against a "
                                                "fresh isolated deployment of one artifact")
    soak_cmd.add_argument("--artifact", required=True, type=Path)
    soak_cmd.add_argument("--fixture-version", default="mesflow-fixture-v1")
    soak_cmd.add_argument("--evidence-root", type=Path, default=Path("reports/qualification"))
    soak_cmd.add_argument("--keep-environment", action="store_true")
    soak_cmd.add_argument("--profile", default="SMALL_FACTORY", choices=["SMALL_FACTORY", "MEDIUM_FACTORY", "LARGE_FACTORY"])
    soak_cmd.add_argument("--speed", default="10X", choices=["REAL_TIME", "2X", "5X", "10X"])
    soak_cmd.add_argument("--window-seconds", type=float, default=60.0)
    soak_cmd.add_argument("--seed", type=int, default=None)

    manifest = commands.add_parser("manifest")
    manifest.add_argument("run_id")
    certify = commands.add_parser("certify")
    certify.add_argument("--artifact-id", required=True)
    certify.add_argument("--environment-id", required=True)
    certify.add_argument("--run-id", action="append", required=True)
    certify.add_argument("--hil-configured", action="store_true")
    args = parser.parse_args(argv)
    service = QualificationService()

    if args.command == "manifest":
        _json(service.run_manifest(args.run_id))
        return 0

    if args.command == "certify":
        decision = evaluate(args.artifact_id, args.environment_id, args.run_id,
                            hil_configured=args.hil_configured)
        _json(decision)
        return 0 if decision["production_eligible"] else 2

    if args.command == "replay-scenario":
        try:
            result = replay_scenario_fn(args.scenario_run_id, args.evidence_root, keep_environment=args.keep_environment)
            _json(result)
            return 0 if result["replay_status"] == "PASSED" else 1
        except Exception as exc:
            _json({"error": str(exc), "error_class": type(exc).__name__})
            return 1

    if args.command in ("hil", "soak"):
        manifest, err_code = _safe_inspect_package(args.artifact)
        if err_code is not None:
            return err_code
        artifact = service.register_artifact(application_version=str(manifest["version"]),
                                             git_commit=str(manifest.get("source_commit") or "unknown"), path=args.artifact)
        environment = service.attest_environment(name=f"{args.command}-{artifact['sha256'][:12]}-{uuid.uuid4().hex[:8]}",
                                                  kind="QA", target_url="http://qualification.invalid",
                                                  database_identity="pending-isolated-postgresql", destructive_allowed=True,
                                                  identity=f"QA:{args.command}:{artifact['sha256']}:{uuid.uuid4().hex[:8]}")
        # soak maps directly onto the SOAK run kind; hil has no exact
        # vocabulary match (spec section 4's list has no HIL-specific
        # entry) -- FUNCTIONAL is the closest honest fit for "one
        # standalone functional-ish suite run", not a fabricated category.
        qualification = service.start_run(artifact_id=artifact["id"], environment_id=environment["id"], profile=args.command,
                                          dataset_version=args.fixture_version, scenario_set_version=args.command,
                                          run_kind="SOAK" if args.command == "soak" else "FUNCTIONAL")
        deployer = ArtifactDeployment(args.evidence_root)
        deployment = None
        try:
            if args.command == "soak":
                deployment = deployer.deploy(qualification["id"], args.artifact, fixture_version=args.fixture_version)
                DeterministicDatabase(args.evidence_root).seed(qualification["id"], deployment["db_container"], args.fixture_version)
                result = LoadSoakRunner(args.evidence_root).run(qualification["id"], deployment, profile=args.profile,
                                                                speed_label=args.speed, window_seconds=args.window_seconds, seed=args.seed)
            else:
                # esp_hil doesn't need its own isolated Postgres/app -- it
                # only needs the artifact registered for identity, plus
                # (optionally) a backend_url the real device can reach.
                result = EspHilRunner(args.evidence_root).run(qualification["id"], backend_url=args.backend_url)
            final = service.finish_run(qualification["id"])
            final["suite_result"] = result
            final["coverage_snapshot"] = coverage_snapshot(qualification["id"])
            _json(final)
            return 0 if result["status"] == "PASSED" else (2 if result["status"] in ("NOT_CONFIGURED", "BLOCKED") else 1)
        except Exception as exc:
            final = service.finish_run(qualification["id"])
            final["error"] = str(exc)
            _json(final)
            return 1
        finally:
            if deployment and not args.keep_environment:
                deployer.destroy(deployment["namespace"])

    if args.command == "run-artifact":
        manifest, err_code = _safe_inspect_package(args.artifact)
        if err_code is not None:
            return err_code
        artifact = service.register_artifact(application_version=str(manifest["version"]),
                                             git_commit=str(manifest.get("source_commit") or "unknown"), path=args.artifact)
        environment = service.attest_environment(name=f"qualification-{artifact['sha256'][:12]}", kind="QA",
                                                  target_url="http://qualification.invalid",
                                                  database_identity="pending-isolated-postgresql", destructive_allowed=True,
                                                  identity=f"QA:artifact:{artifact['sha256']}")
        qualification = service.start_run(artifact_id=artifact["id"], environment_id=environment["id"], profile="full",
                                          dataset_version=args.fixture_version,
                                          scenario_set_version=args.scenario_set_version,
                                          run_kind="RELEASE_QUALIFICATION")
        run_id = qualification["id"]
        deployer = ArtifactDeployment(args.evidence_root)
        deployment = None
        results: dict = {}
        try:
            if not args.skip_build_integrity:
                results["build_integrity"] = _run_suite_or_record_failure(run_id, "build_integrity", "build",
                    lambda: BuildIntegrityRunner(args.evidence_root).run(run_id, args.artifact))
            if not args.skip_critical_unit:
                source_root = str(args.source_root) if args.source_root else os.environ.get("MESFLOW_SOURCE_ROOT", "")
                if source_root:
                    results["critical_unit"] = _run_suite_or_record_failure(run_id, "critical_unit", "unit",
                        lambda: run_critical_unit(run_id, args.evidence_root, source_root=source_root))
                else:
                    results["critical_unit"] = {"status": "BLOCKED",
                        "reason": "no --source-root and no MESFLOW_SOURCE_ROOT env var; critical_unit needs the "
                                  "MESFlow app repo checked out somewhere reachable"}

            deployment = deployer.deploy(run_id, args.artifact, fixture_version=args.fixture_version)
            service.conn.execute("UPDATE qa_environments SET target_url=?,hostname=?,database_identity=?,attested_at=? WHERE id=?",
                                 (deployment["target_url"], deployment["app_container"], deployment["database_identity"],
                                  now(), environment["id"]))
            service.conn.commit()
            DeterministicDatabase(args.evidence_root).seed(run_id, deployment["db_container"], args.fixture_version)

            if not args.skip_post_deploy_smoke:
                results["post_deploy_smoke"] = _run_suite_or_record_failure(run_id, "post_deploy_smoke", "smoke",
                    lambda: PostDeploySmokeRunner(args.evidence_root).run(run_id, deployment["target_url"]))

            live = LiveMESFlowQualification(run_id, deployment["target_url"], deployment["db_container"],
                                            args.evidence_root, intentional_wrong_quantity=args.intentional_wrong_quantity)
            results["api_contract"] = live.api_contracts()
            results["integration"] = live.integration()
            results["mes_workflows"] = live.workflows()
            results["invariants"] = live.invariants()

            if not args.skip_browser:
                results["ui_critical"] = _run_browser_suite_or_record_failure(run_id, deployment["target_url"], args.evidence_root)

            if not args.skip_kiosk_emulator:
                results["kiosk_emulator"] = _run_suite_or_record_failure(run_id, "kiosk_emulator", "kiosk",
                    lambda: KioskEmulatorRunner(args.evidence_root).run(run_id, deployment["target_url"], deployment["db_container"]))

            if not args.skip_recovery:
                results["recovery"] = _run_suite_or_record_failure(run_id, "recovery", "recovery",
                    lambda: RecoveryRunner(args.evidence_root).run(run_id, deployment))

            if not args.skip_test_deployment:
                results["test_deployment"] = _run_suite_or_record_failure(run_id, "test_deployment", "deployment",
                    lambda: TestDeploymentRunner(args.evidence_root).run(run_id, args.artifact, registered_sha256=artifact["sha256"]))

            if args.old_artifact:
                results["upgrade"] = _run_suite_or_record_failure(run_id, "upgrade", "upgrade",
                    lambda: UpgradeRunner(args.evidence_root).run(run_id, args.old_artifact, args.artifact,
                                                                   fixture_version=args.fixture_version))
            else:
                results["upgrade"] = {"status": "NOT_IMPLEMENTED",
                    "reason": "no --old-artifact supplied; upgrade needs a real previous-version artifact to upgrade "
                              "FROM, never a fresh database"}

            if not args.skip_load_soak:
                results["load_soak"] = _run_suite_or_record_failure(run_id, "load_soak", "soak",
                    lambda: LoadSoakRunner(args.evidence_root).run(run_id, deployment, window_seconds=args.soak_window_seconds))
            else:
                results["load_soak"] = {"status": "NOT_IMPLEMENTED", "reason": "--skip-load-soak was passed"}

            if args.enable_hil:
                results["esp_hil"] = _run_suite_or_record_failure(run_id, "esp_hil", "hil",
                    lambda: EspHilRunner(args.evidence_root).run(run_id, backend_url=args.hil_backend_url))
            else:
                results["esp_hil"] = {"status": "NOT_CONFIGURED", "reason": "--enable-hil was not passed for this run"}

            final = service.finish_run(run_id)
            final["deployment"] = deployment
            final["scenario_results"] = results
            final["coverage_snapshot"] = coverage_snapshot(run_id)
            _json(final)
            return 0 if final["status"] == "PASSED" else 1
        except Exception as exc:
            final = service.finish_run(run_id)
            final["error"] = str(exc)
            final["scenario_results"] = results
            try:
                final["coverage_snapshot"] = coverage_snapshot(run_id)
            except Exception:
                pass
            _json(final)
            return 1
        finally:
            if deployment and not args.keep_environment:
                deployer.destroy(deployment["namespace"])

    if args.command == "replay-suite":
        manifest, err_code = _safe_inspect_package(args.artifact)
        if err_code is not None:
            return err_code
        artifact = service.register_artifact(application_version=str(manifest["version"]),
                                             git_commit=str(manifest.get("source_commit") or "unknown"), path=args.artifact)
        environment = service.attest_environment(name=f"replay-{artifact['sha256'][:12]}-{uuid.uuid4().hex[:8]}",
                                                  kind="QA", target_url="http://qualification.invalid",
                                                  database_identity="pending-isolated-postgresql", destructive_allowed=True,
                                                  identity=f"QA:replay:{artifact['sha256']}:{uuid.uuid4().hex[:8]}")
        qualification = service.start_run(artifact_id=artifact["id"], environment_id=environment["id"], profile="replay",
                                          dataset_version=args.fixture_version, scenario_set_version="replay",
                                          run_kind="UI_E2E" if args.suite == "ui_critical" else "FUNCTIONAL")
        deployer = ArtifactDeployment(args.evidence_root)
        deployment = None
        try:
            # Fresh isolated Postgres + a fresh app container from the SAME
            # artifact bytes -- this is the deterministic-fixture reset spec
            # section 8 requires before any replay: a suite is never rerun
            # against whatever state a prior attempt happened to leave
            # behind (the fixture SQL is not idempotent against pre-existing
            # rows, so reusing an old deployment is not an option here).
            deployment = deployer.deploy(qualification["id"], args.artifact, fixture_version=args.fixture_version)
            service.conn.execute("UPDATE qa_environments SET target_url=?,hostname=?,database_identity=?,attested_at=? WHERE id=?",
                                 (deployment["target_url"], deployment["app_container"], deployment["database_identity"],
                                  now(), environment["id"]))
            service.conn.commit()
            DeterministicDatabase(args.evidence_root).seed(qualification["id"], deployment["db_container"], args.fixture_version)
            live = LiveMESFlowQualification(qualification["id"], deployment["target_url"], deployment["db_container"],
                                            args.evidence_root)
            suite_methods = {"api_contract": live.api_contracts, "integration": live.integration,
                             "mes_workflows": live.workflows, "invariants": live.invariants}
            if args.suite == "ui_critical":
                result = _run_browser_suite_or_record_failure(qualification["id"], deployment["target_url"], args.evidence_root)
            else:
                result = suite_methods[args.suite]()
            final = service.finish_run(qualification["id"])
            final["deployment"] = deployment
            final["replayed_suite"] = args.suite
            final["replays_run_id"] = args.replays_run_id
            final["suite_result"] = result
            final["coverage_snapshot"] = coverage_snapshot(qualification["id"])
            _json(final)
            return 0 if final["status"] == "PASSED" else 1
        except Exception as exc:
            final = service.finish_run(qualification["id"])
            final["error"] = str(exc)
            _json(final)
            return 1
        finally:
            if deployment and not args.keep_environment:
                deployer.destroy(deployment["namespace"])

    artifact = service.register_artifact(application_version=args.version,
                                         git_commit=_git_commit(args.source_root), path=args.artifact)
    environment = service.attest_environment(name=args.environment_name, kind=args.environment_kind,
                                              target_url=args.target_url,
                                              database_identity=args.database_identity,
                                              destructive_allowed=args.environment_kind in {"QA", "TEST"})
    qualification = service.start_run(artifact_id=artifact["id"], environment_id=environment["id"],
                                      profile=args.profile, dataset_version=args.dataset_version,
                                      scenario_set_version=args.scenario_set_version)
    config = json.loads(args.suite_config.read_text(encoding="utf-8"))
    runner = QualificationRunner(args.evidence_root)
    for suite in config.get("suites", []):
        profiles = suite.get("profiles", ["quick", "full"])
        if args.profile not in profiles:
            continue
        runner.run_command_suite(qualification["id"], suite_key=suite["key"], layer=suite["layer"],
                                 command=list(suite["command"]), cwd=args.source_root / suite.get("cwd", "."),
                                 required=bool(suite.get("required", True)),
                                 timeout_seconds=int(suite.get("timeout_seconds", 1800)))
    result = service.finish_run(qualification["id"])
    try:
        result["coverage_snapshot"] = coverage_snapshot(qualification["id"])
    except Exception:
        pass
    _json(result)
    return 0 if result["status"] == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
