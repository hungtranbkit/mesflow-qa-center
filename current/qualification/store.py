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
CREATE TABLE IF NOT EXISTS qa_deployments (
  id TEXT PRIMARY KEY, qualification_run_id TEXT NOT NULL REFERENCES qa_qualification_runs(id),
  namespace TEXT NOT NULL UNIQUE, status TEXT NOT NULL, application_container TEXT NOT NULL,
  database_container TEXT NOT NULL, network_name TEXT NOT NULL, volume_name TEXT NOT NULL,
  target_url TEXT NOT NULL DEFAULT '', database_identity TEXT NOT NULL,
  manifest_json TEXT NOT NULL DEFAULT '{}', runtime_json TEXT NOT NULL DEFAULT '{}',
  started_at TEXT NOT NULL, ready_at TEXT, stopped_at TEXT, error TEXT NOT NULL DEFAULT ''
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
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    conn = qa_store.connect()
    conn.executescript(SCHEMA)
    conn.commit()
    return conn
