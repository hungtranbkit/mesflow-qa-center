"""v1.26.0 -- REPORT_30_DAYS productivity dataset + shared-preview reseed hardening.

Covers:
  - the real bug found live (every seeded operation had
    standard_seconds_per_unit=0, so MESFlow's employee-performance report's
    efficiency_percent was always null even with real session history),
  - REPORT_30_DAYS as the standard 25-employee / 30-day productivity
    dataset, generated deterministically from quantity + a real formula
    (never an independently-guessed duration),
  - reset_reseed() as the one true "switch testcase without recreating
    anything" operation (now also able to change preset in place),
  - the exact regression this preset exists to catch:
    completed_sessions > 0 AND productivity_valid_sessions == 0 -> FAIL.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
pytest.importorskip("flask")
from engine.preview import presets as preview_presets
from engine.preview import seed as preview_seed
from engine.preview import expectations as preview_expectations
from engine import coverage_runner, preview_manager
from engine.preview_guard import PreviewSafetyError

ROOT = Path(__file__).resolve().parents[1]


class _FakeCursor:
    """Records every work_sessions INSERT (employee_id/started_at/ended_at)
    plus the operations/production_orders backfill UPDATEs (2026-08-23
    duration-first rewrite backfills done_qty/defect_qty/planned_quantity
    from the actually-generated sessions) so tests can verify no-overlap,
    duration math, and aggregate consistency directly, without a real
    Postgres."""

    def __init__(self):
        self._next_id = 1
        self.work_sessions: list[dict[str, Any]] = []
        self.operation_updates: dict[int, dict[str, int]] = {}
        self.po_planned_quantity: dict[int, int] = {}

    def execute(self, sql, params=None):
        if params is not None and "INSERT INTO work_sessions" in sql:
            self.work_sessions.append({
                "id": self._next_id, "employee_id": params[0], "operation_id": params[1],
                "status": params[4], "started_at": params[5], "ended_at": params[6],
                "good_qty": params[7], "defect_qty": params[8],
            })
        elif params is not None and sql.strip().startswith("UPDATE operations SET done_qty"):
            good, defect, rework, op_id = params
            self.operation_updates[op_id] = {"done_qty": good, "defect_qty": defect, "rework_qty": rework}
        elif params is not None and sql.strip().startswith("UPDATE production_orders SET planned_quantity"):
            planned_quantity, po_id = params
            self.po_planned_quantity[po_id] = planned_quantity
        return None

    def fetchone(self):
        row = (self._next_id,)
        self._next_id += 1
        return row


# --------------------------------------------------------------------------
# Part 2/3: the real bug (standard_seconds_per_unit=0) and REPORT_30_DAYS shape
# --------------------------------------------------------------------------

def test_report_30_days_is_25_employees_30_days_productivity_scale():
    spec = preview_presets.get_spec("REPORT_30_DAYS")
    assert spec.employees == 25
    assert spec.productivity_scale is True


def test_insert_po_with_operation_never_defaults_standard_time_to_zero():
    # Regression: every previously-seeded operation had standard_seconds_per_unit=0
    # (the column was never in the INSERT at all), so MESFlow's efficiency_percent
    # was always null. The default itself must now be a real positive number.
    import inspect
    sig = inspect.signature(preview_seed._insert_po_with_operation)
    assert sig.parameters["standard_seconds_per_unit"].default > 0


def test_operation_types_all_have_a_positive_standard_time():
    for name, lo, hi, dept in preview_seed._OPERATION_TYPES:
        assert lo > 0 and hi >= lo, name
        assert dept


def test_report_30_days_generates_the_requested_scale(rng=None):
    import random
    cur = _FakeCursor()
    employee_ids = list(range(1, 26))
    station_ids = list(range(1, 9))
    report = preview_seed._seed_report_30_days(
        cur, random.Random("REPORT_30_DAYS-seed"), anchor=datetime(2026, 8, 23, 10, 0, 0),
        sales_order_id=1, employee_ids=employee_ids, station_ids=station_ids, preset="REPORT_30_DAYS",
    )
    assert 8 <= report["production_orders"] <= 15 + 2  # +2 for the two deliberate edge-case POs
    assert 20 <= len(report["operations"]) <= 40 + 2
    assert 750 <= len(report["closed_session_ids"]) <= 1125
    assert 6 <= len(report["open_session_ids"]) <= 10
    with_std = sum(1 for o in report["operations"] if o.get("standard_seconds_per_unit", 0) > 0)
    without_std = len(report["operations"]) - with_std
    assert with_std / len(report["operations"]) >= 0.9  # "1-2 operation thiếu standard time" only
    assert 1 <= without_std <= 2
    assert report["history_days"] == 30


def test_report_30_days_full_assertion_list(monkeypatch=None):
    # The exact post-seed assertion list from the task, run against the real
    # generator (fake cursor, real logic).
    import random
    cur = _FakeCursor()
    employee_ids = list(range(1, 26))
    station_ids = list(range(1, 9))
    report = preview_seed._seed_report_30_days(
        cur, random.Random("REPORT_30_DAYS-seed"), anchor=datetime(2026, 8, 23, 10, 0, 0),
        sales_order_id=1, employee_ids=employee_ids, station_ids=station_ids, preset="REPORT_30_DAYS",
    )
    closed = len(report["closed_session_ids"])
    valid = len(report["closed_session_ids"])  # every _seed_productivity_session call is a valid one
    invalid = len(report["invalid_session_ids"])
    total_closed = closed + invalid

    assert len(employee_ids) == 25
    assert closed >= 750
    assert report["min_closed_sessions_per_employee"] >= 30
    assert min(report["employee_targets"]) >= 30 and max(report["employee_targets"]) <= 45
    assert valid / total_closed >= 0.95
    assert report["avg_productivity_percent"] > 80
    assert min(report["productivity_values"]) >= 50 - 1e-9
    assert max(report["productivity_values"]) <= 150 + 1e-9


def test_report_30_days_employee_sessions_never_overlap():
    import random
    cur = _FakeCursor()
    employee_ids = list(range(1, 26))
    station_ids = list(range(1, 9))
    preview_seed._seed_report_30_days(
        cur, random.Random("REPORT_30_DAYS-seed"), anchor=datetime(2026, 8, 23, 10, 0, 0),
        sales_order_id=1, employee_ids=employee_ids, station_ids=station_ids, preset="REPORT_30_DAYS",
    )
    by_employee: dict[int, list[dict]] = {}
    for row in cur.work_sessions:
        if row["status"] == "CLOSED":
            by_employee.setdefault(row["employee_id"], []).append(row)
    assert len(by_employee) == 25  # every employee actually got sessions
    for employee_id, sessions in by_employee.items():
        sessions.sort(key=lambda r: r["started_at"])
        for prev, nxt in zip(sessions, sessions[1:]):
            assert nxt["started_at"] >= prev["ended_at"], (
                f"employee {employee_id}: session starting {nxt['started_at']} overlaps "
                f"one ending {prev['ended_at']}"
            )


def test_report_30_days_working_hours_and_days_off():
    import random
    cur = _FakeCursor()
    employee_ids = list(range(1, 26))
    station_ids = list(range(1, 9))
    preview_seed._seed_report_30_days(
        cur, random.Random("REPORT_30_DAYS-seed"), anchor=datetime(2026, 8, 23, 10, 0, 0),
        sales_order_id=1, employee_ids=employee_ids, station_ids=station_ids, preset="REPORT_30_DAYS",
    )
    closed = [r for r in cur.work_sessions if r["status"] == "CLOSED"]
    # Most sessions start in a normal day-shift window -- a later session
    # pushed by the no-overlap cursor can drift outside it, so "most", not
    # "all".
    in_hours = sum(1 for r in closed if 6 <= r["started_at"].hour <= 20)
    assert in_hours / len(closed) >= 0.8
    # "multiple sessions on some days and zero sessions on some days":
    from collections import Counter
    per_employee_day_counts: dict[int, Counter] = {}
    for r in closed:
        per_employee_day_counts.setdefault(r["employee_id"], Counter())[r["started_at"].date()] += 1
    some_employee_has_a_double_day = any(max(c.values()) >= 2 for c in per_employee_day_counts.values())
    some_employee_has_a_rest_day = any(len(c) < 30 for c in per_employee_day_counts.values())
    assert some_employee_has_a_double_day
    assert some_employee_has_a_rest_day
    # Realistic factory shift windows, not the small hours (task:
    # "Avoid sessions at 02:00 unless intentionally testing night shift") --
    # "most", not "all": the no-overlap cursor can occasionally push a
    # session's actual start past a long prior session, same as the
    # in-hours check above.
    reasonable_hour = sum(1 for r in closed if 5 <= r["started_at"].hour <= 22)
    assert reasonable_hour / len(closed) >= 0.9
    # A no-overlap cursor pushed past midnight by an unusually long prior
    # session (up to 420 min) can rarely land here -- same "avoid impossible
    # overlap takes priority over staying in-hours" tradeoff already
    # documented for this generator; it must stay rare, never dominate.
    small_hours = sum(1 for r in closed if 0 <= r["started_at"].hour < 5)
    assert small_hours / len(closed) < 0.02


# --------------------------------------------------------------------------
# 2026-08-23: worked-hours/duration realism -- the real bug found live
# ("32 sessions, 5h39m total" -- ~10 minutes/session average, unrealistic
# for a 30-day factory productivity dataset). Session count alone is not
# enough any more; these are the task's own required acceptance criteria.
# --------------------------------------------------------------------------

def _seed_full_report():
    cur = _FakeCursor()
    employee_ids = list(range(1, 26))
    station_ids = list(range(1, 9))
    report = preview_seed._seed_report_30_days(
        cur, random.Random("REPORT_30_DAYS-seed"), anchor=datetime(2026, 8, 23, 10, 0, 0),
        sales_order_id=1, employee_ids=employee_ids, station_ids=station_ids, preset="REPORT_30_DAYS",
    )
    return cur, report


def test_worked_hours_per_employee_meet_the_realism_floor():
    cur, report = _seed_full_report()
    hours = report["worked_hours_by_employee"]
    assert len(hours) == 25
    print(f"worked hours/employee: min={min(hours):.1f} avg={sum(hours)/len(hours):.1f} max={max(hours):.1f}")
    assert min(hours) >= 60, "no employee should fall under the 60h/30-day realism floor"
    assert sum(hours) / len(hours) >= 80
    assert max(hours) <= 180, "no employee should exceed ~180h/30 days unless explicitly intended"
    # "some lower-utilization ~60-80h... some highly active 130-150h" --
    # both tails must actually exist, not just the floor/ceiling holding.
    assert any(h < 85 for h in hours)
    assert any(h > 115 for h in hours)


def test_no_30_plus_sessions_with_under_20_hours_total():
    """The exact regression this task exists to prevent: 32 CLOSED sessions
    totaling 5h39m (real bug seen live) must never happen again for any
    employee, at any session count >= 30."""
    cur, report = _seed_full_report()
    by_employee: dict[int, list[dict]] = {}
    for row in cur.work_sessions:
        if row["status"] == "CLOSED":
            by_employee.setdefault(row["employee_id"], []).append(row)
    for employee_id, sessions in by_employee.items():
        if len(sessions) < 30:
            continue
        total_hours = sum((s["ended_at"] - s["started_at"]).total_seconds() for s in sessions) / 3600.0
        assert total_hours >= 20, (
            f"employee {employee_id}: {len(sessions)} CLOSED sessions but only "
            f"{total_hours:.1f}h total -- the exact bug this dataset must never reproduce"
        )


def test_median_session_duration_is_realistic():
    cur, report = _seed_full_report()
    median_min = report["median_session_minutes"]
    print(f"median session minutes: {median_min:.1f}, p90: {report['p90_session_minutes']:.1f}")
    assert 60 <= median_min <= 200, "median session should be a real work block, not 5-15 minutes"
    # "Avoid thousands of 5-15 minute sessions" -- a small minority is fine.
    tiny = sum(1 for m in report["session_minutes"] if m < 15)
    assert tiny / len(report["session_minutes"]) < 0.05


def test_operation_qty_ranges_differ_by_operation_type():
    ranges = preview_seed._OPERATION_QTY_RANGES
    assert len(set(ranges.values())) > 1, "qty ranges must differ per operation type, not one range for all"
    for name, _lo, _hi, _dept in preview_seed._OPERATION_TYPES:
        assert name in ranges, f"{name} has no qty range configured"


def test_qty_from_target_duration_respects_soft_bounds():
    qlo, qhi = 20, 120  # e.g. Cắt laser
    # A very short target duration should never round down to something
    # degenerate (below 1/4 the type's own stated floor).
    small = preview_seed._qty_from_target_duration(45.0, 30.0, 95.0, (qlo, qhi))
    assert small >= max(1, qlo // 4)
    # A very long target duration is allowed to exceed the stated max (task:
    # "adjust... upward as needed"), but not without limit.
    large = preview_seed._qty_from_target_duration(45.0, 20000.0, 60.0, (qlo, qhi))
    assert large <= qhi * 3


def test_aggregate_consistency_operation_done_qty_matches_its_own_sessions():
    """operation.done_qty/defect_qty (backfilled post-hoc from the actually
    -generated sessions) must equal SUM(good_qty)/SUM(defect_qty) over that
    operation's own CLOSED sessions -- the aggregate is never an
    independently-guessed number."""
    cur, report = _seed_full_report()
    by_operation: dict[int, list[dict]] = {}
    for row in cur.work_sessions:
        if row["status"] == "CLOSED":
            by_operation.setdefault(row["operation_id"], []).append(row)
    assert cur.operation_updates, "the backfill UPDATE must actually run"
    mismatches = 0
    for op_id, updated in cur.operation_updates.items():
        sessions = by_operation.get(op_id, [])
        real_good = sum(s["good_qty"] for s in sessions)
        real_defect = sum(s["defect_qty"] for s in sessions)
        if updated["done_qty"] != real_good or updated["defect_qty"] != real_defect:
            mismatches += 1
    assert mismatches == 0


def test_planned_quantity_never_falls_below_done_qty():
    cur, report = _seed_full_report()
    done_by_po: dict[int, int] = {}
    for op in report["operations"]:
        if "po_id" not in op:
            continue  # the 2 deliberately-invalid operations carry no po_id in this dict
        updated = cur.operation_updates.get(op["operation_id"], {"done_qty": 0, "defect_qty": 0})
        done_by_po[op["po_id"]] = done_by_po.get(op["po_id"], 0) + updated["done_qty"] + updated["defect_qty"]
    for po_id, done_total in done_by_po.items():
        planned = cur.po_planned_quantity.get(po_id)
        assert planned is not None and planned >= done_total, (
            f"PO {po_id}: planned_quantity {planned} is below its own operations' done total {done_total}"
        )


def test_report_30_days_deterministic_across_two_runs():
    import random
    employee_ids = list(range(1, 26))
    station_ids = list(range(1, 9))
    anchor = datetime(2026, 8, 23, 10, 0, 0)
    r1 = preview_seed._seed_report_30_days(
        _FakeCursor(), random.Random("REPORT_30_DAYS-seed"), anchor=anchor, sales_order_id=1,
        employee_ids=employee_ids, station_ids=station_ids, preset="REPORT_30_DAYS",
    )
    r2 = preview_seed._seed_report_30_days(
        _FakeCursor(), random.Random("REPORT_30_DAYS-seed"), anchor=anchor, sales_order_id=1,
        employee_ids=employee_ids, station_ids=station_ids, preset="REPORT_30_DAYS",
    )
    assert len(r1["closed_session_ids"]) == len(r2["closed_session_ids"])
    assert len(r1["open_session_ids"]) == len(r2["open_session_ids"])
    assert r1["productivity_values"] == r2["productivity_values"]


def test_25_unique_employee_names_available():
    assert len(preview_seed._VIETNAMESE_NAMES) >= 25
    assert len(set(preview_seed._VIETNAMESE_NAMES)) == len(preview_seed._VIETNAMESE_NAMES)


# --------------------------------------------------------------------------
# Part 4: duration IS derived from quantity + target productivity, never guessed
# --------------------------------------------------------------------------

def test_productivity_session_duration_matches_the_exact_formula():
    import random
    cur = _FakeCursor()
    started = datetime(2026, 8, 20, 8, 0, 0)
    standard = 60.0
    good, defect = 25, 5  # qty = 30
    sid, target_pct, ended = preview_seed._seed_productivity_session(
        cur, random.Random("determinism-check"), employee_id=1, employee_index=0, operation_id=1,
        station_id=1, standard_seconds_per_unit=standard, good_qty=good, defect_qty=defect, started_at=started,
    )
    expected_seconds = standard * (good + defect)
    actual_seconds = expected_seconds / (target_pct / 100.0)
    assert 50.0 <= target_pct <= 150.0  # hard-clamped range
    assert ended == started + timedelta(seconds=actual_seconds)
    # And the row actually inserted agrees with the returned ended_at.
    assert cur.work_sessions[-1]["ended_at"] == ended


def test_productivity_pct_for_is_hard_clamped_to_50_150():
    import random
    rng = random.Random("floor-check")
    for _ in range(500):  # many draws across all employees -- the clamp must hold every time, not just usually
        for i in range(25):
            pct = preview_seed._productivity_pct_for(rng, i)
            assert 50.0 <= pct <= 150.0


def test_employee_baseline_productivity_spans_the_requested_bands():
    baselines = preview_seed._EMPLOYEE_BASELINE_PRODUCTIVITY
    assert len(baselines) == 25
    lt60 = sum(1 for b in baselines if b < 60)
    b60_79 = sum(1 for b in baselines if 60 <= b < 80)
    b80_99 = sum(1 for b in baselines if 80 <= b < 100)
    b100_115 = sum(1 for b in baselines if 100 <= b <= 115)
    gt115 = sum(1 for b in baselines if b > 115)
    assert lt60 + b60_79 + b80_99 + b100_115 + gt115 == 25
    assert lt60 >= 1 and gt115 >= 1  # both edge bands must exist, but only a few


# --------------------------------------------------------------------------
# Part 8: expected-state manifest -- one shared system, no parallel one
# --------------------------------------------------------------------------

def test_manifest_carries_every_requested_productivity_field():
    manifest = preview_expectations.build_manifest("REPORT_30_DAYS", datetime(2026, 8, 23), {
        "employees": 25, "production_orders": 14, "operations": 32,
        "closed_sessions": 323, "open_sessions": 8,
        "productivity_valid_sessions": 319, "productivity_invalid_sessions": 4,
        "operations_with_standard_time": 30, "history_days": 30,
        "avg_productivity_percent_expected_range": [75, 110],
        # Worked-hours realism (2026-08-23).
        "min_worked_hours_per_employee": 62.0, "avg_worked_hours": 97.3, "max_worked_hours": 131.0,
        "median_session_minutes": 133.6, "p90_session_minutes": 291.7,
    })
    scale = manifest["expectations"]["scale"]
    for key in ("employees", "production_orders", "operations", "closed_sessions", "open_sessions",
                "productivity_valid_sessions", "productivity_invalid_sessions",
                "operations_with_standard_time", "history_days",
                "min_worked_hours_per_employee", "avg_worked_hours", "max_worked_hours",
                "median_session_minutes", "p90_session_minutes"):
        assert key in scale, key
    assert scale["avg_productivity_percent_expected_range"] == [75, 110]


def test_validate_flags_worked_hours_below_the_recommended_floor():
    manifest = preview_expectations.build_manifest("REPORT_30_DAYS", datetime(2026, 8, 23), {
        "min_worked_hours_per_employee": 60, "avg_worked_hours": 80, "median_session_minutes": 60,
    })
    # Observed side reproduces the exact historical bug: sessions exist but
    # total worked time is far too low.
    mismatches = preview_expectations.validate(manifest, {
        "scale": {"min_worked_hours_per_employee": 5.6, "avg_worked_hours": 30, "median_session_minutes": 10},
    })
    paths = {m["path"] for m in mismatches}
    assert "scale.min_worked_hours_per_employee" in paths
    assert "scale.avg_worked_hours" in paths
    assert "scale.median_session_minutes" in paths


def test_validate_flags_worked_hours_above_the_180h_ceiling():
    manifest = preview_expectations.build_manifest("REPORT_30_DAYS", datetime(2026, 8, 23), {})
    mismatches = preview_expectations.validate(manifest, {"scale": {"max_worked_hours": 210}})
    assert any(m["path"] == "scale.max_worked_hours" for m in mismatches)


def test_validate_skips_worked_hours_checks_when_not_observed():
    # No live coverage-runner check currently computes these from the real
    # report API -- an absent observation must never be treated as a failure.
    # Only the worked-hours scale fields are declared here (no po/session/
    # exception counts), isolating that specific absence-tolerance behavior
    # from validate()'s separate, pre-existing "expected a count, observed
    # nothing" checks for those other sections.
    manifest = preview_expectations.build_manifest("REPORT_30_DAYS", datetime(2026, 8, 23), {
        "min_worked_hours_per_employee": 60, "avg_worked_hours": 80,
    })
    mismatches = preview_expectations.validate(manifest, {"scale": {}})
    worked_hours_mismatches = [m for m in mismatches if m["path"].startswith("scale.")
                               and "worked_hours" in m["path"]]
    assert worked_hours_mismatches == []


def test_manifest_does_not_create_a_parallel_system_for_full_ui():
    # FULL_UI's own fields (progress_bands/completed_sessions_min/...) still
    # live in the SAME "scale" block, not a second manifest structure.
    manifest = preview_expectations.build_manifest("FULL_UI", datetime(2026, 8, 23), {
        "employees": 25, "completed_sessions_min": 175, "active_sessions_min": 10,
        "operations_with_partial_progress_min": 12, "operations_completed_min": 6,
    })
    assert "scale" in manifest["expectations"]
    assert "employees" in manifest["expectations"]["scale"]


# --------------------------------------------------------------------------
# Part 9: the exact regression -- completed_sessions>0 AND valid=0 -> FAIL
# --------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def test_check_productivity_flags_the_exact_historical_bug():
    findings = coverage_runner._Findings()

    def fake_get(path):
        if path.startswith("/api/reports/employee-performance"):
            # Real response nests everything under "report" (see
            # mesflow/web/analytics.py: jsonify(ok=True, report=report)) --
            # found live, this shape mismatch was itself a real bug in this
            # module's first draft.
            return _FakeResp({"report": {
                "employees": [{"id": 1}],
                # 61 sessions, all CLOSED, all with qty -- but every one's
                # operation is missing standard_seconds_per_unit, exactly the
                # historical bug.
                "sessions": [{"status": "CLOSED", "good_qty": 10, "defect_qty": 0,
                               "standard_seconds_per_unit": 0} for _ in range(61)],
                "summary": {"completed_session_count": 61, "efficiency_percent": None},
            }})
        return None

    coverage_runner._check_productivity(
        fake_get, {"history_days": 30, "productivity_valid_sessions": 250}, "2026-08-23T10:00:00", findings,
    )

    assert any(f["error_type"] == "PRODUCTIVITY_ALWAYS_NULL" for f in findings.items)
    assert any(f["error_type"] == "REPORT_MISMATCH" and "productivity-valid" in f["title"].lower() for f in findings.items)


def test_check_productivity_passes_when_the_report_has_real_data():
    findings = coverage_runner._Findings()

    def fake_get(path):
        if path.startswith("/api/reports/employee-performance"):
            return _FakeResp({"report": {
                "employees": [{"id": 1}, {"id": 2}],
                "sessions": [{"status": "CLOSED", "good_qty": 10, "defect_qty": 1,
                               "standard_seconds_per_unit": 60} for _ in range(300)],
                "summary": {"completed_session_count": 300, "efficiency_percent": 92.5},
            }})
        return None

    coverage_runner._check_productivity(
        fake_get, {"history_days": 30, "productivity_valid_sessions": 250,
                   "avg_productivity_percent_expected_range": [75, 110]}, "2026-08-23T10:00:00", findings,
    )
    assert findings.items == []


def test_check_productivity_is_a_no_op_without_history_days():
    findings = coverage_runner._Findings()

    def boom(path):
        raise AssertionError("must not call the API when this preset has no history_days expectation")

    coverage_runner._check_productivity(boom, {}, "2026-08-23T10:00:00", findings)
    assert findings.items == []


# --------------------------------------------------------------------------
# Part 1 / 14 (5,6,7): reseed reuses everything, can switch preset in place
# --------------------------------------------------------------------------

def _label_json(**kv):
    import json as _json
    return _json.dumps(kv)


class _FakeResultDC:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _fake_ready_env(monkeypatch, calls):
    def fake_runner(args, timeout=60):
        calls.append(args)
        cmd = args[1:]
        verb = cmd[0]
        if verb == "inspect" and len(cmd) >= 3 and "Labels" in cmd[2]:
            return _FakeResultDC(stdout=_label_json(**{"com.mesflow.qa.preview": "1"}) + "\n")
        if verb == "inspect" and len(cmd) >= 3 and "Env" in cmd[2]:
            return _FakeResultDC(stdout='["MESFLOW_UI_PREVIEW=1"]\n')
        if verb == "volume" and cmd[1] == "inspect":
            return _FakeResultDC(stdout="null\n")
        return _FakeResultDC(stdout="ok\n")

    class FakeResp:
        status_code = 200

        def json(self):
            return {"ok": True, "version": "9.9.9"}

    monkeypatch.setattr(preview_manager.requests, "get", lambda *a, **k: FakeResp())
    monkeypatch.setattr(preview_manager.preview_seed, "run_seed", lambda database_url, preset: {"seed_version": "x"})
    mgr = preview_manager.PreviewManager.build(
        docker=preview_manager.DockerCLI(runner=fake_runner), self_container="qa-center-test",
    )
    env_id = mgr.begin_create("EMPTY_STATE", image="mesflow-app:test")
    env = mgr.finish_create(env_id)
    return mgr, env


def test_reseed_never_recreates_the_backend_or_db_container(monkeypatch, tmp_path):
    from engine import qa_store
    qa_store.reset_for_tests(tmp_path / "reseed_test.sqlite3")
    calls = []
    mgr, env = _fake_ready_env(monkeypatch, calls)
    wipe_calls = []
    monkeypatch.setattr(preview_manager.preview_seed, "wipe_runtime_data", lambda url: wipe_calls.append(url))
    calls.clear()

    mgr.reset_reseed(env["id"])

    mutating = {("run",), ("rm",), ("volume", "rm"), ("volume", "create"), ("network", "rm"), ("network", "create")}
    for c in calls:
        verb = tuple(c[1:3]) if c[1] in ("volume", "network") else (c[1],)
        assert verb not in mutating, f"reseed issued a container/network/volume mutation: {c}"
    assert wipe_calls, "reseed must still TRUNCATE via the DB connection, just never touch Docker"


def test_reseed_keeps_the_same_preview_id_port_network_and_credentials(monkeypatch, tmp_path):
    from engine import qa_store
    qa_store.reset_for_tests(tmp_path / "reseed_identity.sqlite3")
    calls = []
    mgr, before = _fake_ready_env(monkeypatch, calls)
    monkeypatch.setattr(preview_manager.preview_seed, "wipe_runtime_data", lambda url: None)

    after = mgr.reset_reseed(before["id"])

    for field in ("id", "port", "network", "app_container", "db_container", "volume", "db_password", "db_name"):
        assert after[field] == before[field], field
    assert after["status"] == "READY"


def test_reseed_can_switch_preset_in_place_without_recreating_anything(monkeypatch, tmp_path):
    from engine import qa_store
    qa_store.reset_for_tests(tmp_path / "reseed_switch.sqlite3")
    calls = []
    mgr, before = _fake_ready_env(monkeypatch, calls)
    monkeypatch.setattr(preview_manager.preview_seed, "wipe_runtime_data", lambda url: None)
    assert before["preset"] == "EMPTY_STATE"
    calls.clear()

    after = mgr.reset_reseed(before["id"], preset="EDGE_CASES")

    assert after["preset"] == "EDGE_CASES"
    assert after["id"] == before["id"] and after["port"] == before["port"] and after["network"] == before["network"]
    mutating = {("run",), ("rm",), ("volume", "rm"), ("volume", "create")}
    for c in calls:
        verb = tuple(c[1:3]) if c[1] == "volume" else (c[1],)
        assert verb not in mutating


def test_reseed_refuses_an_unknown_preset_before_touching_anything(monkeypatch, tmp_path):
    from engine import qa_store
    qa_store.reset_for_tests(tmp_path / "reseed_bad_preset.sqlite3")
    calls = []
    mgr, before = _fake_ready_env(monkeypatch, calls)
    wipe_calls = []
    monkeypatch.setattr(preview_manager.preview_seed, "wipe_runtime_data", lambda url: wipe_calls.append(url))

    with pytest.raises(preview_presets.UnknownPresetError):
        mgr.reset_reseed(before["id"], preset="NOT_A_REAL_PRESET")

    assert not wipe_calls, "must refuse before ever touching the database"
    assert mgr.get(before["id"])["status"] == "READY"  # unchanged, no partial mutation


def test_reseed_refuses_when_the_container_is_missing_the_preview_guard(monkeypatch, tmp_path):
    from engine import qa_store
    qa_store.reset_for_tests(tmp_path / "reseed_guard.sqlite3")
    calls = []

    def unlabeled_runner(args, timeout=60):
        calls.append(args)
        cmd = args[1:]
        if cmd[0] == "inspect" and len(cmd) >= 3 and "Env" in cmd[2]:
            return _FakeResultDC(stdout='[]\n')  # missing MESFLOW_UI_PREVIEW=1
        if cmd[0] == "inspect" and len(cmd) >= 3 and "Labels" in cmd[2]:
            return _FakeResultDC(stdout=_label_json(**{"com.mesflow.qa.preview": "1"}) + "\n")
        return _FakeResultDC(stdout="ok\n")

    class FakeResp:
        status_code = 200

        def json(self):
            return {"ok": True, "version": "9.9.9"}

    monkeypatch.setattr(preview_manager.requests, "get", lambda *a, **k: FakeResp())
    monkeypatch.setattr(preview_manager.preview_seed, "run_seed", lambda database_url, preset: {"seed_version": "x"})
    mgr = preview_manager.PreviewManager.build(docker=preview_manager.DockerCLI(runner=unlabeled_runner))
    env_id = mgr.begin_create("EMPTY_STATE", image="mesflow-app:test")
    mgr.finish_create(env_id)

    with pytest.raises(PreviewSafetyError):
        mgr.reset_reseed(env_id)


# --------------------------------------------------------------------------
# Admin password
# --------------------------------------------------------------------------

def test_preview_admin_password_is_the_fixed_known_value():
    assert preview_manager.PREVIEW_ADMIN_PASSWORD == "1234567890"
    assert len(preview_manager.PREVIEW_ADMIN_PASSWORD) >= 10  # MESFlow's own production bootstrap floor
