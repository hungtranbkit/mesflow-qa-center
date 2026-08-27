"""Expected-state manifest (requirement 12).

The seed script is the one place that knows exactly what it put in the
database. It writes that down as a manifest; the coverage runner (or any
future browser/API verifier) compares what MESFlow actually shows against
this manifest instead of scattering hard-coded assertions everywhere.

All comparisons are "at least" (>=): the seed guarantees a minimum number
of late/warning/completed/etc. rows exist, never an exact ceiling.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

SEED_VERSION = "1"


def build_manifest(preset: str, anchor: datetime, counts: dict[str, int]) -> dict[str, Any]:
    expectations: dict[str, Any] = {
        "production_orders": {
            "late": counts.get("po_late", 0),
            "warning": counts.get("po_warning", 0),
            "completed": counts.get("po_completed", 0),
        },
        "sessions": {
            "open": counts.get("sessions_open", 0),
            "long_open": counts.get("sessions_long_open", 0),
            "closed": counts.get("sessions_closed", 0),
        },
        "exceptions": {"minimum": counts.get("exceptions", 0)},
    }
    # Factory-scale expectations -- one shared "scale" block for every
    # realistic-scale generator (FULL_UI's progress bands, REPORT_30_DAYS's
    # productivity fields), never a second parallel manifest system. Only
    # present when the seed actually generated these counts; a preset that
    # never sets any of them simply gets no "scale" key, and validate()
    # skips it below.
    scale_keys = (
        "employees", "completed_sessions_min", "active_sessions_min",
        "operations_with_partial_progress_min", "operations_completed_min",
        "production_orders", "operations", "closed_sessions", "open_sessions",
        "productivity_valid_sessions", "productivity_invalid_sessions",
        "operations_with_standard_time", "history_days",
        "min_closed_sessions_per_employee", "avg_productivity_percent",
        # Worked-hours realism (2026-08-23): session count alone no longer
        # proves the dataset looks like a real factory month -- see the
        # task's "TOTAL WORKED TIME VALIDATION" / "TESTS / MANIFEST" sections.
        "min_worked_hours_per_employee", "avg_worked_hours", "max_worked_hours",
        "median_session_minutes", "p90_session_minutes",
    )
    if any(k in counts for k in scale_keys):
        expectations["scale"] = {k: counts[k] for k in scale_keys if k in counts}
        if counts.get("progress_bands"):
            expectations["scale"]["progress_bands"] = counts["progress_bands"]
        if counts.get("avg_productivity_percent_expected_range"):
            expectations["scale"]["avg_productivity_percent_expected_range"] = counts["avg_productivity_percent_expected_range"]
    return {
        "preset": preset,
        "anchor": anchor.isoformat(timespec="seconds"),
        "seed_version": SEED_VERSION,
        "expectations": expectations,
    }


def validate(manifest: dict[str, Any], observed: dict[str, Any]) -> list[dict[str, Any]]:
    """Return a list of mismatches (empty = everything the seed promised is present)."""
    mismatches: list[dict[str, Any]] = []
    exp = manifest.get("expectations", {}) or {}

    po_exp = exp.get("production_orders", {}) or {}
    po_obs = observed.get("production_orders", {}) or {}
    for key in ("late", "warning", "completed"):
        want = po_exp.get(key)
        if want is None:
            continue
        got = po_obs.get(key)
        if got is None or got < want:
            mismatches.append({"path": f"production_orders.{key}", "expected": want, "observed": got})

    sess_exp = exp.get("sessions", {}) or {}
    sess_obs = observed.get("sessions", {}) or {}
    for key in ("open", "long_open", "closed"):
        want = sess_exp.get(key)
        if want is None:
            continue
        got = sess_obs.get(key)
        if got is None or got < want:
            mismatches.append({"path": f"sessions.{key}", "expected": want, "observed": got})

    exc_exp = exp.get("exceptions", {}) or {}
    if "minimum" in exc_exp:
        want = exc_exp["minimum"]
        got = (observed.get("exceptions") or {}).get("count")
        if got is None or got < want:
            mismatches.append({"path": "exceptions.minimum", "expected": want, "observed": got})

    scale_exp = exp.get("scale") or {}
    scale_obs = observed.get("scale") or {}
    for key in ("employees", "completed_sessions_min", "active_sessions_min",
                "operations_with_partial_progress_min", "operations_completed_min",
                "production_orders", "operations", "closed_sessions", "open_sessions",
                "productivity_valid_sessions", "operations_with_standard_time",
                "min_closed_sessions_per_employee"):
        want = scale_exp.get(key)
        if not want:
            continue
        got = scale_obs.get(key.removesuffix("_min"))
        if got is None or got < want:
            mismatches.append({"path": f"scale.{key}", "expected": want, "observed": got})

    pct_range = scale_exp.get("avg_productivity_percent_expected_range")
    avg_pct = scale_obs.get("avg_productivity_percent")
    if pct_range and avg_pct is not None:
        lo, hi = pct_range
        if not (lo <= avg_pct <= hi):
            mismatches.append({"path": "scale.avg_productivity_percent_expected_range",
                                "expected": pct_range, "observed": avg_pct})

    # Worked-hours realism (2026-08-23): "at least" checks like the loop
    # above, but skipped (not failed) when the observed side hasn't been
    # computed yet -- no live coverage-runner check currently populates
    # these from the real report API, so an absent observation means
    # "not verified this round", never "the seed promised nothing".
    for key in ("min_worked_hours_per_employee", "avg_worked_hours", "median_session_minutes"):
        want = scale_exp.get(key)
        got = scale_obs.get(key)
        if want is None or got is None:
            continue
        if got < want:
            mismatches.append({"path": f"scale.{key}", "expected": want, "observed": got})

    # max_worked_hours is a CEILING, not a floor -- "no employee above ~180
    # hours in 30 days unless explicitly intended".
    max_hours = scale_obs.get("max_worked_hours")
    if max_hours is not None and max_hours > 180:
        mismatches.append({"path": "scale.max_worked_hours", "expected": "<= 180", "observed": max_hours})

    return mismatches
