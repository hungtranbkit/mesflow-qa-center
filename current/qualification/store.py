from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from engine import qa_store


SCHEMA = """
CREATE TABLE IF NOT EXISTS qa_releases (
  id TEXT PRIMARY KEY, application_version TEXT NOT NULL, git_commit TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS qa_artifacts (
  id TEXT PRIMARY KEY, release_id TEXT NOT NULL REFERENCES qa_releases(id),
  filename TEXT NOT NULL, sha256 TEXT NOT NULL UNIQUE, size_bytes INTEGER NOT NULL,
  media_type TEXT NOT NULL DEFAULT 'application/octet-stream', source_path TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS qa_environments (
  id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, identity TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL CHECK(kind IN ('LOCAL','QA','TEST','PRODUCTION_TEST','PRODUCTION')),
  target_url TEXT NOT NULL, hostname TEXT NOT NULL, database_identity TEXT NOT NULL,
  destructive_allowed INTEGER NOT NULL DEFAULT 0, attested_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS qa_qualification_runs (
  id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL REFERENCES qa_artifacts(id),
  environment_id TEXT NOT NULL REFERENCES qa_environments(id), profile TEXT NOT NULL,
  dataset_version TEXT NOT NULL, scenario_set_version TEXT NOT NULL,
  status TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
  retries INTEGER NOT NULL DEFAULT 0, failed_fingerprints TEXT NOT NULL DEFAULT '[]',
  summary_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_qa_runs_artifact ON qa_qualification_runs(artifact_id,started_at);
CREATE TABLE IF NOT EXISTS qa_suite_runs (
  id TEXT PRIMARY KEY, qualification_run_id TEXT NOT NULL REFERENCES qa_qualification_runs(id),
  suite_key TEXT NOT NULL, layer TEXT NOT NULL, required INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
  command_json TEXT NOT NULL DEFAULT '[]', exit_code INTEGER, summary_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS qa_scenario_runs (
  id TEXT PRIMARY KEY, suite_run_id TEXT NOT NULL REFERENCES qa_suite_runs(id),
  scenario_key TEXT NOT NULL, scenario_version TEXT NOT NULL, driver TEXT NOT NULL,
  status TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
  first_failing_step TEXT NOT NULL DEFAULT '', fingerprint TEXT NOT NULL DEFAULT '',
  expected_json TEXT NOT NULL DEFAULT '{}', actual_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS qa_attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, scenario_run_id TEXT NOT NULL REFERENCES qa_scenario_runs(id),
  attempt_no INTEGER NOT NULL, status TEXT NOT NULL, fingerprint TEXT NOT NULL DEFAULT '',
  started_at TEXT NOT NULL, finished_at TEXT NOT NULL,
  UNIQUE(scenario_run_id,attempt_no)
);
CREATE TABLE IF NOT EXISTS qa_evidence (
  id TEXT PRIMARY KEY, qualification_run_id TEXT NOT NULL REFERENCES qa_qualification_runs(id),
  suite_run_id TEXT, scenario_run_id TEXT, kind TEXT NOT NULL, filename TEXT NOT NULL,
  sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL, relative_path TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS qa_invariant_results (
  id INTEGER PRIMARY KEY AUTOINCREMENT, qualification_run_id TEXT NOT NULL REFERENCES qa_qualification_runs(id),
  scenario_run_id TEXT, invariant_key TEXT NOT NULL, status TEXT NOT NULL,
  expected_json TEXT NOT NULL DEFAULT '{}', actual_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS qa_certifications (
  id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL REFERENCES qa_artifacts(id),
  environment_id TEXT NOT NULL REFERENCES qa_environments(id), policy_key TEXT NOT NULL,
  policy_version TEXT NOT NULL, status TEXT NOT NULL, production_eligible INTEGER NOT NULL,
  qualification_run_ids TEXT NOT NULL, decision_json TEXT NOT NULL,
  certified_at TEXT NOT NULL, invalidated_at TEXT,
  UNIQUE(artifact_id,environment_id,policy_key,policy_version)
);
-- Also the Sandbox Manager's own registry (spec: "Sandbox Manager
-- consolidation") -- qa_deployments IS the sandbox table; sandbox_id ==
-- this row's id, no second table was introduced. sandbox_type/
-- environment_type/health/retained_at/destroyed_at are additive columns
-- (see _apply_migrations below) so an existing installed database keeps
-- working without a destructive migration.
CREATE TABLE IF NOT EXISTS qa_deployments (
  id TEXT PRIMARY KEY, qualification_run_id TEXT NOT NULL REFERENCES qa_qualification_runs(id),
  namespace TEXT NOT NULL UNIQUE, status TEXT NOT NULL, application_container TEXT NOT NULL,
  database_container TEXT NOT NULL, network_name TEXT NOT NULL, volume_name TEXT NOT NULL,
  target_url TEXT NOT NULL DEFAULT '', database_identity TEXT NOT NULL,
  manifest_json TEXT NOT NULL DEFAULT '{}', runtime_json TEXT NOT NULL DEFAULT '{}',
  started_at TEXT NOT NULL, ready_at TEXT, stopped_at TEXT, error TEXT NOT NULL DEFAULT '',
  sandbox_type TEXT NOT NULL DEFAULT 'EPHEMERAL', environment_type TEXT NOT NULL DEFAULT 'QA',
  project TEXT NOT NULL DEFAULT 'mesflow', version TEXT NOT NULL DEFAULT '',
  artifact_sha256 TEXT NOT NULL DEFAULT '', app_port INTEGER, health TEXT NOT NULL DEFAULT '',
  retained_at TEXT, destroyed_at TEXT
);
-- Persisted at the end of every completed run (see coverage.snapshot()) so
-- feature coverage remains auditable after the run's own ephemeral Docker
-- resources (and even its evidence files, if ever pruned) are gone -- never
-- recomputed against a scratch/temporary database after the fact.
CREATE TABLE IF NOT EXISTS qa_coverage_snapshots (
  id TEXT PRIMARY KEY, qualification_run_id TEXT NOT NULL UNIQUE REFERENCES qa_qualification_runs(id),
  artifact_sha256 TEXT NOT NULL, registry_version TEXT NOT NULL, registry_sha256 TEXT NOT NULL,
  total_features INTEGER NOT NULL, critical_features INTEGER NOT NULL,
  covered_features INTEGER NOT NULL, partially_covered_features INTEGER NOT NULL,
  uncovered_features INTEGER NOT NULL, blocked_features INTEGER NOT NULL,
  critical_covered_features INTEGER NOT NULL, snapshot_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_qa_coverage_snapshots_artifact ON qa_coverage_snapshots(artifact_sha256);
-- Links a scenario-level replay attempt back to the original failure
-- WITHOUT ever mutating the original run's own rows (spec: "Do not mutate
-- the original qualification run"). The replay itself is a completely
-- normal new qualification_run/suite_run/scenario_run (fresh isolated
-- deployment, fresh fixture seed) -- this table is purely the traceability
-- link the UI/API can follow in either direction.
CREATE TABLE IF NOT EXISTS qa_replays (
  id TEXT PRIMARY KEY, original_scenario_run_id TEXT NOT NULL REFERENCES qa_scenario_runs(id),
  replay_qualification_run_id TEXT NOT NULL REFERENCES qa_qualification_runs(id),
  replay_scenario_run_id TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_qa_replays_original ON qa_replays(original_scenario_run_id);
-- Resource Sampler (spec: "Resource Sampler"): one reusable sampling
-- service, one table, usable by LONG_RUNNING_FACTORY_SIMULATION/LOAD/
-- SOAK/CHAOS/RELEASE_QUALIFICATION alike -- see resource_sampler.py.
CREATE TABLE IF NOT EXISTS qa_resource_samples (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, sandbox_id TEXT NOT NULL,
  sampled_at TEXT NOT NULL, metrics_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_qa_resource_samples_run ON qa_resource_samples(run_id, sampled_at);
"""

# Additive-only migrations for databases created before a given column
# existed -- CREATE TABLE IF NOT EXISTS above never alters an
# already-existing table, so a real long-lived install needs these to ever
# see new columns. Mirrors engine/qa_store.py's own _apply_migrations
# pattern (list of ALTER TABLE ADD COLUMN, applied idempotently).
_MIGRATIONS = [
    "ALTER TABLE qa_deployments ADD COLUMN sandbox_type TEXT NOT NULL DEFAULT 'EPHEMERAL'",
    "ALTER TABLE qa_deployments ADD COLUMN environment_type TEXT NOT NULL DEFAULT 'QA'",
    "ALTER TABLE qa_deployments ADD COLUMN project TEXT NOT NULL DEFAULT 'mesflow'",
    "ALTER TABLE qa_deployments ADD COLUMN version TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE qa_deployments ADD COLUMN artifact_sha256 TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE qa_deployments ADD COLUMN app_port INTEGER",
    "ALTER TABLE qa_deployments ADD COLUMN health TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE qa_deployments ADD COLUMN retained_at TEXT",
    "ALTER TABLE qa_deployments ADD COLUMN destroyed_at TEXT",
    "ALTER TABLE qa_qualification_runs ADD COLUMN run_kind TEXT NOT NULL DEFAULT 'RELEASE_QUALIFICATION'",
]


def _apply_migrations(conn: sqlite3.Connection) -> None:
    for statement in _MIGRATIONS:
        try:
            conn.execute(statement)
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise
    conn.commit()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    conn = qa_store.connect()
    conn.executescript(SCHEMA)
    conn.commit()
    _apply_migrations(conn)
    return conn
