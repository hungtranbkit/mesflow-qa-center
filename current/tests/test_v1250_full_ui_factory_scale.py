"""v1.25.0 -- FULL_UI factory-scale dataset.

FULL_UI must look like a real running factory: >=25 employees, 150-250
completed sessions with real multi-session work history, 6-12 active
sessions, and operations deliberately spread across every completion-%%
band -- with the session history and the operation's own done_qty/defect_qty
aggregate always in agreement (never guessed independently). These tests
exercise the actual generation functions against a fake cursor (no real
Postgres needed -- the SQL itself was verified once by hand against the
real MESFlow schema; see README_V1250).
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
pytest.importorskip("flask")
from engine.preview import presets as preview_presets
from engine.preview import seed as preview_seed
from engine.preview import expectations as preview_expectations
from engine import coverage_runner

ROOT = Path(__file__).resolve().parents[1]


class _FakeCursor:
    """Just enough of psycopg's cursor protocol for the seed helpers: every
    INSERT here ends in `RETURNING id`, immediately followed by fetchone()."""

    def __init__(self):
        self._next_id = 1

    def execute(self, sql, params=None):
        return None

    def fetchone(self):
        row = (self._next_id,)
        self._next_id += 1
        return row


@pytest.fixture
def rng():
    import random
    return random.Random("FULL_UI-seed")


def test_full_ui_preset_targets_25_employees_and_realistic_scale():
    spec = preview_presets.get_spec("FULL_UI")
    assert spec.employees >= 25
    assert spec.realistic_scale is True


def test_other_presets_are_untouched_by_the_realistic_scale_path():
    for key in ("NORMAL_FACTORY", "PROBLEM_FACTORY", "REPORT_30_DAYS", "EMPTY_STATE", "EDGE_CASES"):
        spec = preview_presets.get_spec(key)
        assert spec.realistic_scale is False


def test_operation_plan_covers_every_required_progress_band():
    bands = [row[5] for row in preview_seed._FULL_UI_OPERATION_PLAN]
    assert bands.count("0%") >= 1
    partial = sum(1 for b in bands if b in ("10-20%", "30-50%", "60-80%", "90-99%"))
    assert partial >= 10
    assert bands.count("100%") >= 5
    assert bands.count(">100%") >= 1


def test_realistic_operations_generate_150_to_250_completed_sessions(rng):
    cur = _FakeCursor()
    employee_ids = list(range(1, 26))
    station_ids = list(range(1, 7))
    result = preview_seed._seed_realistic_operations(
        cur, rng, anchor=datetime(2026, 8, 23, 10, 0, 0), sales_order_id=1,
        employee_ids=employee_ids, station_ids=station_ids, preset="FULL_UI",
    )
    assert len(result["operations"]) == len(preview_seed._FULL_UI_OPERATION_PLAN)
    assert 150 <= result["completed_session_count"] <= 250
    assert result["band_counts"].get("100%", 0) >= 5
    partial = sum(v for k, v in result["band_counts"].items() if k not in ("0%", "100%", ">100%"))
    assert partial >= 10


def test_realistic_active_sessions_within_6_to_12_and_split_working_idle(rng):
    cur = _FakeCursor()
    employee_ids = list(range(1, 26))
    station_ids = list(range(1, 7))
    ops = preview_seed._seed_realistic_operations(
        cur, rng, anchor=datetime(2026, 8, 23, 10, 0, 0), sales_order_id=1,
        employee_ids=employee_ids, station_ids=station_ids, preset="FULL_UI",
    )
    active = preview_seed._seed_realistic_active_sessions(
        cur, rng, anchor=datetime(2026, 8, 23, 10, 0, 0), operations=ops["operations"],
        employee_ids=employee_ids, station_ids=station_ids,
    )
    assert 6 <= active["active_session_count"] <= 12
    assert 8 <= active["working_employees"] <= 12
    assert 3 <= active["idle_employees"] <= 5


def test_deterministic_across_two_runs_with_the_same_seed():
    import random
    cur1, cur2 = _FakeCursor(), _FakeCursor()
    employee_ids = list(range(1, 26))
    station_ids = list(range(1, 7))
    anchor = datetime(2026, 8, 23, 10, 0, 0)
    r1 = preview_seed._seed_realistic_operations(
        cur1, random.Random("FULL_UI-seed"), anchor=anchor, sales_order_id=1,
        employee_ids=employee_ids, station_ids=station_ids, preset="FULL_UI",
    )
    r2 = preview_seed._seed_realistic_operations(
        cur2, random.Random("FULL_UI-seed"), anchor=anchor, sales_order_id=1,
        employee_ids=employee_ids, station_ids=station_ids, preset="FULL_UI",
    )
    assert r1["completed_session_count"] == r2["completed_session_count"]
    assert r1["band_counts"] == r2["band_counts"]


def test_expectations_manifest_carries_the_scale_block():
    manifest = preview_expectations.build_manifest("FULL_UI", datetime(2026, 8, 23), {
        "employees": 25, "completed_sessions_min": 180, "active_sessions_min": 10,
        "operations_with_partial_progress_min": 12, "operations_completed_min": 6,
        "progress_bands": {"0%": 1, "100%": 6},
    })
    scale = manifest["expectations"]["scale"]
    assert scale["employees"] == 25
    assert scale["completed_sessions_min"] == 180
    assert scale["progress_bands"]["100%"] == 6


def test_expectations_manifest_omits_scale_for_legacy_presets():
    manifest = preview_expectations.build_manifest("NORMAL_FACTORY", datetime(2026, 8, 23), {"po_completed": 2})
    assert "scale" not in manifest["expectations"]


def test_expectations_validate_flags_a_factory_scale_shortfall():
    manifest = preview_expectations.build_manifest("FULL_UI", datetime(2026, 8, 23), {
        "employees": 25, "completed_sessions_min": 180, "active_sessions_min": 10,
        "operations_with_partial_progress_min": 12, "operations_completed_min": 6,
    })
    short = preview_expectations.validate(manifest, {"scale": {
        "employees": 20, "completed_sessions": 180, "active_sessions": 10,
        "operations_with_partial_progress": 12, "operations_completed": 6,
    }})
    assert any(m["path"] == "scale.employees" for m in short)

    ok = preview_expectations.validate(manifest, {"scale": {
        "employees": 25, "completed_sessions": 190, "active_sessions": 11,
        "operations_with_partial_progress": 14, "operations_completed": 7,
    }})
    assert [m for m in ok if m["path"].startswith("scale.")] == []


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def test_check_factory_scale_flags_employee_and_active_session_shortfalls():
    findings = coverage_runner._Findings()

    def fake_get(path):
        if path == "/api/employees":
            return _FakeResp({"items": [{"id": i} for i in range(20)]})  # short of 25
        if path == "/api/dashboard/active-sessions":
            return _FakeResp({"items": [{"id": i} for i in range(3)]})  # short of 6
        if path.startswith("/api/kpi/operations"):
            return _FakeResp({"items": []})
        return None

    coverage_runner._check_factory_scale(fake_get, {
        "employees": 25, "active_sessions_min": 6,
        "operations_with_partial_progress_min": 10, "operations_completed_min": 5,
    }, findings)

    titles = [f["title"] for f in findings.items]
    assert any("employees" in t.lower() for t in titles)
    assert any("active sessions" in t.lower() for t in titles)


def test_check_factory_scale_flags_completion_percent_disagreeing_with_raw_quantities():
    findings = coverage_runner._Findings()

    def fake_get(path):
        if path.startswith("/api/kpi/operations"):
            return _FakeResp({"items": [
                {"code": "OP-1", "plan_qty": 100, "done_qty": 50, "completion_percent": 12.0},  # should be 50.0
            ]})
        return None

    coverage_runner._check_factory_scale(fake_get, {
        "operations_with_partial_progress_min": 1,
    }, findings)

    assert any(f["error_type"] == "REPORT_MISMATCH" and "completion_percent" in f["title"] for f in findings.items)


def test_check_factory_scale_is_a_no_op_without_scale_expectations():
    findings = coverage_runner._Findings()

    def boom(path):
        raise AssertionError("must not call the API when there is nothing to check")

    coverage_runner._check_factory_scale(boom, {}, findings)
    assert findings.items == []
