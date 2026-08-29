"""Run policy: FAST UI / REGRESSION / FULL (requirement 11).

This module never silently skips a feature -- every decision carries a
human-readable reason so the UI can show it (item 11: "Không silently
skip").
"""
from __future__ import annotations

import sqlite3
from typing import Any

from . import bug_store
from . import feature_registry as fr
from . import qa_store

FAST = "FAST"
REGRESSION = "REGRESSION"
FULL = "FULL"
MODES = (FAST, REGRESSION, FULL)

RUN = "RUN"
SKIP_STABLE = "SKIP_STABLE"


def _regression_test_cases_by_feature(conn: sqlite3.Connection) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for bug in bug_store.list_bugs(conn=conn):
        tc = bug.get("regression_test_case") or ""
        feature = bug.get("feature") or ""
        if tc and feature:
            out.setdefault(feature, [])
            if tc not in out[feature]:
                out[feature].append(tc)
    return out


def _features_with_unresolved_regression(conn: sqlite3.Connection) -> set[str]:
    return {b["feature"] for b in bug_store.list_bugs(status=bug_store.REGRESSION, conn=conn) if b.get("feature")}


def regression_cases_for(feature_key: str, conn: sqlite3.Connection | None = None) -> list[str]:
    """Public accessor (requirement 8/10): the regression test cases history
    has attached to a feature, derived from bug_store rather than duplicated
    onto the feature row -- bug_store.record verify(passed=True) is the one
    place a regression case is ever attached, so this stays the single
    source of truth instead of two copies that could drift."""
    conn = conn or qa_store.connect()
    return _regression_test_cases_by_feature(conn).get(feature_key, [])


def compute_run_plan(
    mode: str, *, impacted_features: list[str] | None = None, conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    conn = conn or qa_store.connect()
    features = fr.list_features(conn=conn)
    regression_tests_by_feature = _regression_test_cases_by_feature(conn)
    unresolved_regression_features = _features_with_unresolved_regression(conn)
    impacted = set(impacted_features or [])
    features_with_history = set(regression_tests_by_feature)

    decisions: list[dict[str, Any]] = []
    mandatory_regression_test_cases: list[str] = []

    for feature in features:
        key = feature["key"]
        status = feature["status"]
        history_tests = regression_tests_by_feature.get(key, [])

        if mode == FULL:
            decisions.append({"feature": key, "action": RUN, "reason": "FULL mode runs every feature"})
            mandatory_regression_test_cases.extend(history_tests)
            continue

        if mode == REGRESSION:
            if key in impacted or key in features_with_history or key in unresolved_regression_features or status != fr.STABLE:
                reason = "impacted by source change" if key in impacted else (
                    "has historical regression test cases" if key in history_tests else (
                        "unresolved regression bug" if key in unresolved_regression_features else f"status={status}"
                    )
                )
                decisions.append({"feature": key, "action": RUN, "reason": reason})
                mandatory_regression_test_cases.extend(history_tests)
            else:
                decisions.append({
                    "feature": key, "action": SKIP_STABLE,
                    "reason": "No related source changes since last verified commit",
                })
            continue

        # FAST
        if status == fr.STABLE:
            decisions.append({
                "feature": key, "action": SKIP_STABLE,
                "reason": "No related source changes since last verified commit",
            })
        elif status in (fr.NEW, fr.CHANGED, fr.REGRESSION_RISK, fr.BROKEN):
            decisions.append({"feature": key, "action": RUN, "reason": f"status={status}"})
            mandatory_regression_test_cases.extend(history_tests)
        else:  # COVERED
            decisions.append({"feature": key, "action": RUN, "reason": "not yet STABLE, needs more coverage"})

    return {
        "mode": mode,
        "decisions": decisions,
        "mandatory_regression_test_cases": sorted(set(mandatory_regression_test_cases)),
        "global_smoke": mode in (FAST, REGRESSION, FULL),
        "skip_count": sum(1 for d in decisions if d["action"] == SKIP_STABLE),
        "run_count": sum(1 for d in decisions if d["action"] == RUN),
    }
