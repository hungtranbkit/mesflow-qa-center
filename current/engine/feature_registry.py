"""Feature Stability / Regression Protection registry (requirement 2-4).

A feature moves:

    NEW -> COVERED -> STABLE
                         |  (source touched)          -> CHANGED
                         |  (unresolved/regressed bug) -> REGRESSION_RISK
                         |  (a recorded test FAILs)    -> BROKEN

STABLE is only granted when ALL of:
  1. it has at least one declared test case,
  2. the most recent recorded runs of those test cases are consecutive PASSes,
  3. that streak has reached ``min_consecutive_passes``,
  4. there is no unresolved bug (OPEN/FIXING/READY_FOR_VERIFY/REGRESSION) tied
     to this feature,
  5. it has a non-empty source-path mapping,
  6. there is no open coverage gap (a declared behavior tag with no test case
     covering it).

Never auto-promoted by a single PASS -- see ``record_result``.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from . import bug_store
from . import qa_store

NEW = "NEW"
COVERED = "COVERED"
STABLE = "STABLE"
CHANGED = "CHANGED"
REGRESSION_RISK = "REGRESSION_RISK"
BROKEN = "BROKEN"

UNRESOLVED_BUG_STATUSES = (bug_store.OPEN, bug_store.FIXING, bug_store.READY_FOR_VERIFY, bug_store.REGRESSION)

# Seeded once, on first run, when the registry table is empty. Source paths
# below were confirmed against the real MESFlow app tree
# (app/mesflow/web/analytics.py etc.) -- see AGENTS.md before changing them.
DEFAULT_FEATURES: list[dict[str, Any]] = [
    {
        "key": "dashboard.production_overview",
        "source_paths": [
            "app/mesflow/web/analytics.py",
            "app/mesflow/db/repositories/analytics.py",
        ],
        "test_cases": ["ui.dashboard.production_overview", "api.dashboard.summary"],
        "behavior_tags": ["po_totals", "active_sessions", "open_critical_events"],
    },
    {
        "key": "session.finish",
        "source_paths": [
            "app/mesflow/web/execution.py",
            "app/mesflow/db/repositories/execution.py",
        ],
        "test_cases": ["session.finish.normal", "session.finish.zero_qty", "session.finish.defect"],
        "behavior_tags": ["normal", "zero_qty", "defect"],
    },
]


class ImpactDetectionError(Exception):
    pass


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _row(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    for field in ("source_paths", "test_cases", "behavior_tags"):
        try:
            d[field] = json.loads(d.get(field) or "[]")
        except (TypeError, ValueError):
            d[field] = []
    d["needs_regression"] = bool(d.get("needs_regression"))
    d["coverage_gap"] = bool(d.get("coverage_gap"))
    return d


def ensure_bootstrapped(conn: sqlite3.Connection | None = None) -> None:
    conn = conn or qa_store.connect()
    count = conn.execute("SELECT COUNT(*) c FROM features").fetchone()["c"]
    if count:
        return
    for spec in DEFAULT_FEATURES:
        upsert_feature(
            spec["key"], source_paths=spec["source_paths"], test_cases=spec["test_cases"],
            behavior_tags=spec.get("behavior_tags", []), conn=conn,
        )


def upsert_feature(
    key: str, *, source_paths: list[str] | None = None, test_cases: list[str] | None = None,
    behavior_tags: list[str] | None = None, min_consecutive_passes: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    conn = conn or qa_store.connect()
    existing = conn.execute("SELECT * FROM features WHERE key=?", (key,)).fetchone()
    now = _now()
    if existing is None:
        conn.execute(
            """INSERT INTO features(key,status,source_paths,test_cases,behavior_tags,
                   min_consecutive_passes,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (key, NEW, json.dumps(source_paths or []), json.dumps(test_cases or []),
             json.dumps(behavior_tags or []), min_consecutive_passes or 3, now, now),
        )
    else:
        merged_sources = source_paths if source_paths is not None else json.loads(existing["source_paths"] or "[]")
        merged_tests = test_cases if test_cases is not None else json.loads(existing["test_cases"] or "[]")
        merged_tags = behavior_tags if behavior_tags is not None else json.loads(existing["behavior_tags"] or "[]")
        conn.execute(
            """UPDATE features SET source_paths=?,test_cases=?,behavior_tags=?,
                   min_consecutive_passes=COALESCE(?,min_consecutive_passes),updated_at=? WHERE key=?""",
            (json.dumps(merged_sources), json.dumps(merged_tests), json.dumps(merged_tags),
             min_consecutive_passes, now, key),
        )
    conn.commit()
    return get_feature(key, conn=conn)  # type: ignore[return-value]


def get_feature(key: str, conn: sqlite3.Connection | None = None) -> dict[str, Any] | None:
    conn = conn or qa_store.connect()
    r = conn.execute("SELECT * FROM features WHERE key=?", (key,)).fetchone()
    return _row(r) if r else None


def list_features(conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    conn = conn or qa_store.connect()
    ensure_bootstrapped(conn=conn)
    rows = conn.execute("SELECT * FROM features ORDER BY key").fetchall()
    return [_row(r) for r in rows]


def attach_regression_test_case(feature_key: str, test_case: str, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """Permanently attach a (usually newly created) regression test case to a
    feature after a bug fix verifies PASS (requirement 9)."""
    conn = conn or qa_store.connect()
    feature = get_feature(feature_key, conn=conn)
    if feature is None:
        feature = upsert_feature(feature_key, conn=conn)
    tests = list(feature["test_cases"])
    if test_case not in tests:
        tests.append(test_case)
        upsert_feature(feature_key, test_cases=tests, conn=conn)
    return get_feature(feature_key, conn=conn)  # type: ignore[return-value]


def declare_behavior(feature_key: str, behavior_tag: str, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """Declare a new behavior/branch/state for a feature, then re-check for a
    coverage gap (requirement 4)."""
    conn = conn or qa_store.connect()
    feature = get_feature(feature_key, conn=conn) or upsert_feature(feature_key, conn=conn)
    tags = list(feature["behavior_tags"])
    if behavior_tag not in tags:
        tags.append(behavior_tag)
        upsert_feature(feature_key, behavior_tags=tags, conn=conn)
    return check_coverage_gap(feature_key, conn=conn)


def _tag_covered(tag: str, test_cases: list[str]) -> bool:
    return any(tag == segment for tc in test_cases for segment in str(tc).split("."))


def check_coverage_gap(feature_key: str, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    conn = conn or qa_store.connect()
    feature = get_feature(feature_key, conn=conn)
    if feature is None:
        raise ValueError(f"Unknown feature {feature_key}")
    missing = [t for t in feature["behavior_tags"] if not _tag_covered(t, feature["test_cases"])]
    gap = bool(missing)
    conn.execute(
        "UPDATE features SET coverage_gap=?,coverage_gap_detail=?,updated_at=? WHERE key=?",
        (1 if gap else 0, json.dumps(missing), _now(), feature_key),
    )
    conn.commit()
    return get_feature(feature_key, conn=conn)  # type: ignore[return-value]


def record_result(
    feature_key: str, test_case: str, result: str, *, run_id: str = "", commit_sha: str = "",
    detail: str = "", conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Record one PASS/FAIL for a test case and re-evaluate stability.

    A single PASS never jumps a feature straight to STABLE -- it only grows
    the consecutive-pass streak; ``evaluate_stability`` decides promotion.
    """
    conn = conn or qa_store.connect()
    result = result.upper()
    if result not in ("PASS", "FAIL"):
        raise ValueError("result must be PASS or FAIL")
    feature = get_feature(feature_key, conn=conn) or upsert_feature(feature_key, conn=conn)
    now = _now()
    conn.execute(
        "INSERT INTO test_case_runs(feature_key,test_case,result,run_id,commit_sha,detail,created_at) VALUES(?,?,?,?,?,?,?)",
        (feature_key, test_case, result, run_id, commit_sha, detail, now),
    )
    if result == "FAIL":
        conn.execute(
            "UPDATE features SET consecutive_passes=0,status=?,last_bug_at=?,updated_at=? WHERE key=?",
            (BROKEN, now, now, feature_key),
        )
        conn.commit()
        return get_feature(feature_key, conn=conn)  # type: ignore[return-value]

    new_status = feature["status"]
    if new_status in (NEW, BROKEN, REGRESSION_RISK):
        new_status = COVERED
    conn.execute(
        """UPDATE features SET consecutive_passes=consecutive_passes+1,last_pass_at=?,
               last_verified_commit=COALESCE(NULLIF(?,''),last_verified_commit),status=?,updated_at=?
           WHERE key=?""",
        (now, commit_sha, new_status, now, feature_key),
    )
    conn.commit()
    return evaluate_stability(feature_key, conn=conn)


def evaluate_stability(feature_key: str, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    conn = conn or qa_store.connect()
    feature = get_feature(feature_key, conn=conn)
    if feature is None:
        raise ValueError(f"Unknown feature {feature_key}")
    if feature["status"] in (STABLE,):
        return feature
    has_tests = bool(feature["test_cases"])
    has_sources = bool(feature["source_paths"])
    enough_passes = feature["consecutive_passes"] >= feature["min_consecutive_passes"]
    unresolved = bug_store.list_bugs(feature=feature_key, conn=conn)
    no_unresolved_bugs = not any(b["status"] in UNRESOLVED_BUG_STATUSES for b in unresolved)
    no_coverage_gap = not feature["coverage_gap"]
    no_pending_regression = not feature["needs_regression"]
    if has_tests and has_sources and enough_passes and no_unresolved_bugs and no_coverage_gap and no_pending_regression:
        now = _now()
        conn.execute(
            "UPDATE features SET status=?,stable_since=?,updated_at=? WHERE key=?",
            (STABLE, now, now, feature_key),
        )
        conn.commit()
    return get_feature(feature_key, conn=conn)  # type: ignore[return-value]


def mark_changed(feature_key: str, *, commit_sha: str = "", conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """A feature's own source changed: it can no longer be considered STABLE
    until a regression run passes again (requirement 3)."""
    conn = conn or qa_store.connect()
    feature = get_feature(feature_key, conn=conn) or upsert_feature(feature_key, conn=conn)
    if feature["status"] == BROKEN:
        return feature  # already the worst state; don't mask it as merely CHANGED
    now = _now()
    conn.execute(
        "UPDATE features SET status=?,needs_regression=1,consecutive_passes=0,updated_at=? WHERE key=?",
        (CHANGED, now, feature_key),
    )
    conn.commit()
    return get_feature(feature_key, conn=conn)  # type: ignore[return-value]


def clear_needs_regression(feature_key: str, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """Called once the mandatory regression run for a CHANGED feature passes."""
    conn = conn or qa_store.connect()
    conn.execute("UPDATE features SET needs_regression=0,updated_at=? WHERE key=?", (_now(), feature_key))
    conn.commit()
    return evaluate_stability(feature_key, conn=conn)


def mark_regression_risk(feature_key: str, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """A REGRESSION-status bug now taints its feature, even if source untouched."""
    conn = conn or qa_store.connect()
    feature = get_feature(feature_key, conn=conn) or upsert_feature(feature_key, conn=conn)
    if feature["status"] in (BROKEN, CHANGED):
        return feature
    now = _now()
    conn.execute("UPDATE features SET status=?,updated_at=? WHERE key=?", (REGRESSION_RISK, now, feature_key))
    conn.commit()
    return get_feature(feature_key, conn=conn)  # type: ignore[return-value]


def git_diff_files(repo_root: Path, previous_commit: str, current_commit: str) -> list[str]:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "diff", "--name-only", previous_commit, current_commit],
            check=True, capture_output=True, text=True, timeout=30,
        )
    except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired) as exc:
        raise ImpactDetectionError(f"git diff failed: {exc}") from exc
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


def compute_impact(
    previous_commit: str, current_commit: str, *, repo_root: Path, conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Map changed files (git diff previous_good_commit..current_commit) onto
    the feature registry's declared source_paths (requirement 3)."""
    conn = conn or qa_store.connect()
    changed_files = git_diff_files(repo_root, previous_commit, current_commit)
    changed_set = set(changed_files)
    impacted: list[str] = []
    for feature in list_features(conn=conn):
        touched = [p for p in feature["source_paths"] if p in changed_set]
        if touched:
            mark_changed(feature["key"], commit_sha=current_commit, conn=conn)
            impacted.append(feature["key"])
    return {
        "previous_commit": previous_commit,
        "current_commit": current_commit,
        "changed_files": changed_files,
        "impacted_features": impacted,
    }
