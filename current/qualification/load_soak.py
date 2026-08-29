"""load_soak: production-policy suite reusing the EXISTING virtual-factory
simulation engine (engine/simulation/*, already deterministic-seeded,
already speed-accelerated, already used by QA Center's own "Simulation"
tab against a UI Preview Lab environment) rather than building a second
simulator -- retargeted here at a real qualification-created isolated
deployment instead of a Preview Lab one. RunManager itself has no
Preview-Lab-specific coupling (it only ever needs base_url/admin_password),
so this is a real reuse, not a workaround.

Honest scope: this is a BOUNDED soak smoke test, not the full 8-hour
production soak the engine also supports. No DURATION_SECONDS preset
shorter than 8 real simulated hours exists, and running one to completion
would make every qualification run take hours -- this suite starts a real
simulation run, lets it execute for a fixed REAL wall-clock window, stops
it, and evaluates whatever real activity/errors accumulated. The seed and
the engine's own run_id are always persisted, so the exact same run is
re-runnable to completion separately, on purpose, when a real multi-hour
soak is actually wanted (not this suite's job).
"""
from __future__ import annotations

import json
import random
import time
import uuid
from pathlib import Path
from typing import Any

from .evidence import EvidenceStore
from .integrity_runner import IntegrityRunner
from .resource_sampler import ResourceSampler
from .store import connect, now

try:
    from engine.simulation.run_manager import RunManager
except Exception:  # pragma: no cover -- engine/ has its own heavier deps (requests etc.)
    RunManager = None


class LoadSoakRunner:
    def __init__(self, evidence_root: Path):
        self.conn = connect()
        self.evidence = EvidenceStore(evidence_root)
        self.evidence_root = evidence_root
        self._integrity = IntegrityRunner(evidence_root)
        self._sampler = ResourceSampler(evidence_root)

    def run(self, run_id: str, deployment: dict[str, Any], *, profile: str = "SMALL_FACTORY",
            speed_label: str = "10X", window_seconds: float = 60.0, seed: int | None = None,
            admin_password: str = "Admin@123456") -> dict[str, Any]:
        suite_id = f"suite-{uuid.uuid4().hex}"
        self.conn.execute("""INSERT INTO qa_suite_runs(id,qualification_run_id,suite_key,layer,required,status,
          started_at,command_json) VALUES(?,?,'load_soak','soak',1,'RUNNING',?,'[]')""", (suite_id, run_id, now()))
        self.conn.commit()

        if RunManager is None:
            return self._finish(suite_id, run_id, "BLOCKED",
                                {"reason": "engine.simulation.run_manager import failed -- see server logs"})

        seed = seed if seed is not None else random.SystemRandom().randrange(1, 2**31)
        target_url = deployment["target_url"]
        mgr = RunManager()
        try:
            snapshot = mgr.start(base_url=target_url, admin_password=admin_password, profile=profile,
                                 duration_label="8_HOURS", speed_label=speed_label, seed=seed)
            sim_run_id = snapshot["run_id"]
        except Exception as exc:
            return self._finish(suite_id, run_id, "FAILED",
                                {"reason": f"simulation failed to bootstrap: {type(exc).__name__}: {exc}", "seed": seed})

        # Resource Sampler (spec section 7): sampled on the same tick as the
        # existing status poll below -- one shared sampling service instead
        # of a second parallel timing loop, persisted by run_id/sandbox_id
        # so it outlives this suite's own return value.
        sandbox_id = deployment.get("id", "")
        resource_samples: list[dict[str, Any]] = []
        deadline = time.time() + window_seconds
        last_snapshot = snapshot
        while time.time() < deadline:
            time.sleep(2)
            resource_samples.append(self._sampler.sample_once(
                run_id, sandbox_id, app_container=deployment.get("app_container", ""),
                db_container=deployment["db_container"], target_url=target_url))
            current = mgr.status()
            if current is None or current["status"] != "RUNNING":
                last_snapshot = current or last_snapshot
                break
            last_snapshot = current
        resource_summary = self._sampler.summarize(resource_samples)

        try:
            final_snapshot = mgr.stop("load_soak qualification window elapsed")
        except RuntimeError:
            final_snapshot = last_snapshot  # already stopped itself (e.g. hit a safety cap)

        metrics = final_snapshot.get("metrics", {})
        # Shared IntegrityRunner (spec: "Integrity Runner consolidation")
        # instead of this suite's own inline 2-check dict (which was a
        # strict subset of live.py's 5-check set and, like recovery.py's
        # old copy, never persisted anything to qa_invariant_results).
        # Soak now gets the full check set and every result is queryable
        # evidence afterwards, not just whatever made it into this payload.
        try:
            results = self._integrity.check(run_id, deployment["db_container"], evidence_name="load-soak-invariants")
            broken = {key: value["violations"] for key, value in results.items() if value["status"] == "FAILED"}
        except Exception as exc:
            broken = {"CHECK_FAILED": [{"error": str(exc)}]}

        payload = {"seed": seed, "engine_run_id": sim_run_id, "profile": profile, "speed_label": speed_label,
                  "window_seconds": window_seconds, "final_snapshot": final_snapshot, "metrics": metrics,
                  "invariant_violations": broken, "resource_samples": resource_summary,
                  "replay_hint": f"engine.simulation.run_manager.RunManager().start(base_url=<same-artifact-deployment>, "
                                 f"admin_password='Admin@123456', profile={profile!r}, duration_label='8_HOURS', "
                                 f"speed_label={speed_label!r}, seed={seed}) reproduces the exact same actor schedule"}
        status = "PASSED"
        if broken:
            status = "FAILED"
        elif metrics.get("errors", 0) > 0:
            status = "FAILED"
        elif metrics.get("business_events", 0) == 0:
            status = "FAILED"  # nothing actually happened -- a silent no-op must not read as a clean pass
        return self._finish(suite_id, run_id, status, payload)

    def _finish(self, suite_id: str, run_id: str, status: str, payload: dict[str, Any]) -> dict[str, Any]:
        evidence = self.evidence.write_json(run_id, "load-soak-result.json", payload, kind="LOAD_SOAK_EVIDENCE", suite_run_id=suite_id)
        self.conn.execute("UPDATE qa_suite_runs SET status=?,finished_at=?,summary_json=? WHERE id=?",
                          (status, now(), json.dumps({"evidence_id": evidence["id"], "seed": payload.get("seed")}), suite_id))
        self.conn.commit()
        return {"suite_id": suite_id, "status": status, "evidence": evidence, **payload}
