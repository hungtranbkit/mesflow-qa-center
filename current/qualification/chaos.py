"""First-class CHAOS producer using the shared recovery fault machinery."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .evidence import EvidenceStore
from .recovery import RecoveryRunner
from .resource_sampler import ResourceSampler
from .scenario_runner import ScenarioRunner


class ChaosRunner(RecoveryRunner):
    """Run controlled faults only against namespace-owned QA sandboxes.

    RecoveryRunner remains the compatibility entry point for the release
    qualification suite; CHAOS delegates to exactly those fault actions and
    safety checks instead of copying container-control logic.
    """

    def __init__(self, evidence_root: Path):
        super().__init__(evidence_root)
        self._runner = ScenarioRunner(evidence_root, scenario_version="chaos-v1",
                                      driver="CHAOS", evidence_kind="CHAOS_EVIDENCE")
        self._sampler = ResourceSampler(evidence_root)

    def run(self, run_id: str, deployment: dict[str, Any]) -> dict[str, Any]:
        before = self._sampler.sample_once(
            run_id, deployment.get("id", ""), app_container=deployment["app_container"],
            db_container=deployment["db_container"], target_url=deployment["target_url"])
        result = super().run(run_id, deployment, suite_key="chaos")
        after = self._sampler.sample_once(
            run_id, deployment.get("id", ""), app_container=deployment["app_container"],
            db_container=deployment["db_container"], target_url=deployment["target_url"])
        result["resource_samples"] = {"before": before, "after": after}
        EvidenceStore(self.evidence_root).write_json(
            run_id, "chaos-summary.json",
            {"status": result["status"], "sandbox_id": deployment.get("id"),
             "namespace": deployment.get("namespace"), "resource_samples": result["resource_samples"]},
            kind="CHAOS_EVIDENCE")
        return result
