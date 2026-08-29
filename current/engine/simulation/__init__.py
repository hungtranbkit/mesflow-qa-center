"""LONG_RUNNING_FACTORY_SIMULATION (Phase A).

A persistent, realistically-paced actor simulation for continuous soak
testing of a live MESFlow preview environment -- distinct from
engine/scenario_generator.py's one-shot combinatorial fuzzing engine
(different concern: temporal realism/soak vs. broad state-space coverage).

Phase A (this package, current state) implements:
  - a resumable scheduler keyed by each actor's own next_action_at
    (scheduler.py)
  - a real-HTTP MESFlow client using only public API surfaces, never direct
    DB writes for business behavior (mesflow_client.py)
  - realistic session-duration/productivity/GOOD-DEFECT-REWORK models
    (distributions.py)
  - employee/supervisor/kiosk-device actors with behavior profiles
    (actors/)
  - a one-time realistic factory bootstrap (employees, templates, POs)
    (factory_model.py)
  - a lightweight dashboard/session reconciliation check, reusing the
    existing bug_store for incident dedup/continue-on-failure
    (reconciliation.py)
  - the run orchestrator: bootstrap, tick loop, checkpoint, stop, resume
    (run_manager.py)

NOT yet implemented (explicitly deferred, see the project's own phase plan):
  network fault injection wiring, WIP/material-flow constraints, PO
  edge-scan matrix, exception-engine coverage, production-trace
  reconciliation, multi-day resume soak testing, the full rich QA Center
  UI (a minimal status page/route exists instead).
"""
