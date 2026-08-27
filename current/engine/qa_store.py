"""Shared SQLite metadata store for QA Center's own state.

This is QA Center's metadata database -- NOT a MESFlow database. It holds:
  - preview environments (UI Preview Lab)
  - feature registry (Regression Protection)
  - test case run history
  - bug records (Bug Center)

It lives under MESFLOW_QA_STATE_DIR (the same volume-mounted /data/state
directory QA Center already uses for run state), so it survives container
restarts/redeploys without any extra volume wiring.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

_DEFAULT_DIR = Path(__file__).resolve().parent.parent / "runtime"
STATE_DIR = Path(os.environ.get("MESFLOW_QA_STATE_DIR", str(_DEFAULT_DIR)))
DB_PATH = Path(os.environ.get("MESFLOW_QA_META_DB", str(STATE_DIR / "qa_meta.sqlite3")))

_lock = threading.RLock()
_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS features (
  key TEXT PRIMARY KEY,
  status TEXT NOT NULL DEFAULT 'NEW',
  source_paths TEXT NOT NULL DEFAULT '[]',
  test_cases TEXT NOT NULL DEFAULT '[]',
  behavior_tags TEXT NOT NULL DEFAULT '[]',
  consecutive_passes INTEGER NOT NULL DEFAULT 0,
  min_consecutive_passes INTEGER NOT NULL DEFAULT 3,
  stable_since TEXT,
  last_verified_commit TEXT,
  last_pass_at TEXT,
  last_bug_at TEXT,
  needs_regression INTEGER NOT NULL DEFAULT 0,
  coverage_gap INTEGER NOT NULL DEFAULT 0,
  coverage_gap_detail TEXT NOT NULL DEFAULT '',
  notes TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS test_case_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  feature_key TEXT NOT NULL,
  test_case TEXT NOT NULL,
  result TEXT NOT NULL,
  run_id TEXT NOT NULL DEFAULT '',
  commit_sha TEXT NOT NULL DEFAULT '',
  detail TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_test_case_runs_feature ON test_case_runs(feature_key, created_at);

CREATE TABLE IF NOT EXISTS bugs (
  bug_id TEXT PRIMARY KEY,
  fingerprint TEXT NOT NULL,
  feature TEXT NOT NULL DEFAULT '',
  severity TEXT NOT NULL DEFAULT 'MEDIUM',
  type TEXT NOT NULL DEFAULT 'UNKNOWN',
  title TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'OPEN',
  expected TEXT NOT NULL DEFAULT '',
  actual TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL DEFAULT '',
  api TEXT NOT NULL DEFAULT '',
  evidence_json TEXT NOT NULL DEFAULT '{}',
  run_id TEXT NOT NULL DEFAULT '',
  regression_test_case TEXT NOT NULL DEFAULT '',
  verify_result TEXT NOT NULL DEFAULT '',
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  occurrences INTEGER NOT NULL DEFAULT 1,
  resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_bugs_fingerprint ON bugs(fingerprint);
CREATE INDEX IF NOT EXISTS idx_bugs_status ON bugs(status);
CREATE INDEX IF NOT EXISTS idx_bugs_feature ON bugs(feature);

CREATE TABLE IF NOT EXISTS preview_environments (
  id TEXT PRIMARY KEY,
  image TEXT NOT NULL,
  preset TEXT NOT NULL,
  port INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'CREATING',
  db_name TEXT NOT NULL,
  app_container TEXT NOT NULL,
  db_container TEXT NOT NULL,
  network TEXT NOT NULL,
  volume TEXT NOT NULL DEFAULT '',
  admin_password TEXT NOT NULL DEFAULT '',
  db_password TEXT NOT NULL DEFAULT '',
  mesflow_version TEXT NOT NULL DEFAULT '',
  seed_version TEXT NOT NULL DEFAULT '',
  manifest_json TEXT NOT NULL DEFAULT '{}',
  last_error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS coverage_runs (
  run_id TEXT PRIMARY KEY,
  preview_id TEXT NOT NULL,
  preset TEXT NOT NULL DEFAULT '',
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL DEFAULT 'RUNNING',
  bug_count INTEGER NOT NULL DEFAULT 0,
  checks_json TEXT NOT NULL DEFAULT '[]',
  summary_json TEXT NOT NULL DEFAULT '{}'
);

-- LONG_RUNNING_FACTORY_SIMULATION (Phase A, see engine/simulation/).
CREATE TABLE IF NOT EXISTS sim_runs (
  run_id TEXT PRIMARY KEY,
  preview_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'RUNNING',
  profile TEXT NOT NULL DEFAULT 'SMALL_FACTORY',
  duration_label TEXT NOT NULL DEFAULT '',
  speed REAL NOT NULL DEFAULT 1.0,
  seed INTEGER NOT NULL,
  planned_end_sim_at REAL,
  stop_reason TEXT NOT NULL DEFAULT '',
  clock_json TEXT NOT NULL DEFAULT '{}',
  factory_json TEXT NOT NULL DEFAULT '{}',
  metrics_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sim_runs_status ON sim_runs(status);

CREATE TABLE IF NOT EXISTS sim_actors (
  actor_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  next_action_at REAL NOT NULL,
  state_json TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sim_actors_run ON sim_actors(run_id);

CREATE TABLE IF NOT EXISTS sim_checkpoints (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'LIGHT',
  metrics_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sim_checkpoints_run ON sim_checkpoints(run_id, created_at);
"""


# Additive, idempotent migrations for columns added after a table's initial
# CREATE TABLE IF NOT EXISTS (which only ever runs once per database file --
# it never alters an existing table). Each entry is safe to re-run forever:
# SQLite raises "duplicate column name" if it's already there, which is
# swallowed on purpose.
_MIGRATIONS: list[str] = [
    "ALTER TABLE preview_environments ADD COLUMN volume TEXT NOT NULL DEFAULT ''",
]


def _apply_migrations(conn: sqlite3.Connection) -> None:
    for statement in _MIGRATIONS:
        try:
            conn.execute(statement)
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise
    conn.commit()


def connect() -> sqlite3.Connection:
    """Return a connection for the current thread, creating the schema once."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        return conn
    with _lock:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH), timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA)
        conn.commit()
        _apply_migrations(conn)
        _local.conn = conn
        return conn


def reset_for_tests(path: Path) -> None:
    """Point this process at a fresh throwaway DB file (unit tests only)."""
    global DB_PATH
    DB_PATH = path
    _local.conn = None
