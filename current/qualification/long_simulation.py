"""First-class LONG_RUNNING_FACTORY_SIMULATION qualification profiles."""
from __future__ import annotations

import json
import random
import time
import uuid
from pathlib import Path
from typing import Any

from engine.simulation.run_manager import RunManager

from .evidence import EvidenceStore
from .integrity_runner import IntegrityRunner
from .resource_sampler import ResourceSampler
from .scenario_runner import ScenarioRunner
from .service import QualificationService
from .store import connect, now

PROFILES: dict[str, dict[str, Any]] = {
    "SMOKE": {"duration": "8_HOURS", "speed": "1000X", "factory": "SMALL_FACTORY", "wall_timeout": 75},
    "RELEASE": {"duration": "24_HOURS", "speed": "1000X", "factory": "MEDIUM_FACTORY", "wall_timeout": 180},
    "NIGHTLY": {"duration": "8_HOURS", "speed": "REAL_TIME", "factory": "MEDIUM_FACTORY", "wall_timeout": 8 * 3600 + 600},
    "EXTENDED_24H": {"duration": "24_HOURS", "speed": "REAL_TIME", "factory": "LARGE_FACTORY", "wall_timeout": 24 * 3600 + 900},
    "EXTENDED_3D": {"duration": "3_DAYS", "speed": "REAL_TIME", "factory": "LARGE_FACTORY", "wall_timeout": 3 * 86400 + 1800},
    "EXTENDED_7D": {"duration": "7_DAYS", "speed": "REAL_TIME", "factory": "LARGE_FACTORY", "wall_timeout": 7 * 86400 + 3600},
    "CONTINUOUS": {"duration": "CONTINUOUS", "speed": "REAL_TIME", "factory": "LARGE_FACTORY", "wall_timeout": None},
}


class LongSimulationRunner:
    def __init__(self, evidence_root: Path):
        self.conn = connect()
        self.evidence_root = evidence_root
        self.evidence = EvidenceStore(evidence_root)
        self.integrity = IntegrityRunner(evidence_root)
        self.sampler = ResourceSampler(evidence_root)
        self.scenarios = ScenarioRunner(evidence_root, scenario_version="long-simulation-v1",
                                        driver="API", evidence_kind="LONG_SIMULATION_EVIDENCE")

    def run(self, run_id: str, deployment: dict[str, Any], *, profile: str = "SMOKE",
            seed: int | None = None, accelerated: bool | None = None,
            stop_after_wall_seconds: float | None = None) -> dict[str, Any]:
        name = profile.upper()
        if name not in PROFILES:
            raise ValueError(f"unknown long simulation profile {profile!r}: expected one of {sorted(PROFILES)}")
        spec = dict(PROFILES[name])
        if accelerated is True and spec["speed"] == "REAL_TIME":
            spec["speed"] = "1000X"
            spec["wall_timeout"] = max(120, int({"8_HOURS": 30, "24_HOURS": 90, "3_DAYS": 300, "7_DAYS": 660,
                                                 "CONTINUOUS": 120}[spec["duration"]] * 2))
        elif accelerated is False:
            spec["speed"] = "REAL_TIME"
        seed = seed if seed is not None else random.SystemRandom().randrange(1, 2**31)
        suite_id = f"suite-{uuid.uuid4().hex}"
        self.conn.execute("""INSERT INTO qa_suite_runs(id,qualification_run_id,suite_key,layer,required,status,
          started_at,command_json) VALUES(?,?,'long_running_factory_simulation','simulation',1,'RUNNING',?,?)""",
          (suite_id, run_id, now(), json.dumps({"profile": name, "seed": seed, **spec})))
        self.conn.commit()
        manager = RunManager()
        started_wall = time.monotonic()
        progress = QualificationService()

        def execute():
            initial = manager.start(base_url=deployment["target_url"], admin_password="Admin@123456",
                                    profile=spec["factory"], duration_label=spec["duration"],
                                    speed_label=spec["speed"], seed=seed)
            progress.touch_progress(run_id, phase="long_running_factory_simulation", profile=name,
                                    simulated_time_seconds=initial.get("sim_now"), wall_elapsed_seconds=0)
            samples = []
            deadline = None if spec["wall_timeout"] is None else started_wall + spec["wall_timeout"]
            if stop_after_wall_seconds is not None:
                deadline = started_wall + stop_after_wall_seconds
            while True:
                snapshot = manager.status()
                if snapshot and snapshot["status"] != "RUNNING":
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    if spec["duration"] == "CONTINUOUS" or stop_after_wall_seconds is not None:
                        snapshot = manager.stop("bounded operator stop")
                        break
                    raise AssertionError(f"simulation did not finish within profile wall timeout: {spec}")
                if not samples or time.monotonic() - samples[-1][0] >= 10:
                    metric = self.sampler.sample_once(
                        run_id, deployment.get("id", ""), app_container=deployment["app_container"],
                        db_container=deployment["db_container"], target_url=deployment["target_url"])
                    samples.append((time.monotonic(), metric))
                    # Live observation (spec section 2/6): real, already-
                    # computed snapshot fields only -- RunManager has no
                    # per-actor "current action" text to report (checked;
                    # it genuinely doesn't track one), so that field is
                    # simply never set here rather than invented.
                    if snapshot:
                        progress.touch_progress(run_id, phase="long_running_factory_simulation", profile=name,
                                                simulated_time_seconds=snapshot.get("sim_now"),
                                                wall_elapsed_seconds=round(time.monotonic() - started_wall, 1),
                                                sessions_started=snapshot.get("metrics", {}).get("sessions_started"),
                                                sessions_finished=snapshot.get("metrics", {}).get("sessions_finished"),
                                                employees=snapshot.get("employees"), kiosks=snapshot.get("kiosks"))
                time.sleep(0.5)
            final = manager.status() or snapshot
            checks = self.integrity.assert_ok(run_id, deployment["db_container"], context="post-long-simulation")
            return {"initial": initial, "final": final, "resource_sample_count": len(samples),
                    "integrity": {key: value["status"] for key, value in checks.items()}}

        result = self.scenarios.run(suite_id, run_id, "sessions.lifecycle.long_running_factory_simulation", execute)
        status = result["status"]
        final = result.get("actual", {}).get("final", {})
        metrics = final.get("metrics", {})
        summary = {"status": status, "profile": name, "seed": seed, "factory_profile": spec["factory"],
                   "duration": spec["duration"], "speed": spec["speed"],
                   "simulated_duration_seconds": None if spec["duration"] == "CONTINUOUS" else {
                       "8_HOURS": 28800, "24_HOURS": 86400, "3_DAYS": 259200, "7_DAYS": 604800}[spec["duration"]],
                   "wall_duration_seconds": round(time.monotonic() - started_wall, 3),
                   "event_counts": metrics, "persona_counts": {
                       "employees": final.get("employees", 0), "web_users": final.get("web_users", 0),
                       "kiosks": final.get("kiosks", 0)}, "incidents": metrics.get("errors", 0)}
        self.evidence.write_json(run_id, "long-simulation-summary.json", summary, kind="LONG_SIMULATION_EVIDENCE")
        self.conn.execute("UPDATE qa_suite_runs SET status=?,finished_at=?,summary_json=? WHERE id=?",
                          (status, now(), json.dumps(summary, default=str), suite_id))
        self.conn.commit()
        return {"suite_id": suite_id, "status": status, "scenario": result, "summary": summary}
