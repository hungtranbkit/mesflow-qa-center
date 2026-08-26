"""Phase A of LONG_RUNNING_FACTORY_SIMULATION: scheduler, clock,
distributions, and reconciliation logic. No live MESFlow/Docker needed --
see docs/... or the session's own final report for the live validation run
performed separately against a real preview environment."""
from __future__ import annotations

import random
import time

import pytest

from engine.simulation import distributions
from engine.simulation.clock import SimClock
from engine.simulation.scheduler import Scheduler, run_until_empty_or_stopped


# --------------------------------------------------------------------------
# Scheduler (item 30): priority-queue-based, never a tight poll loop.
# --------------------------------------------------------------------------

def test_scheduler_pops_in_time_order_not_insertion_order():
    s = Scheduler()
    s.schedule("c", 30.0)
    s.schedule("a", 10.0)
    s.schedule("b", 20.0)
    assert s.pop_due(35.0) == ["a", "b", "c"]


def test_scheduler_pop_due_respects_max_batch():
    s = Scheduler()
    for i in range(10):
        s.schedule(f"actor{i}", float(i))
    due = s.pop_due(100.0, max_batch=3)
    assert len(due) == 3
    assert len(s) == 7  # the rest stay queued, drained on later ticks -- no burst


def test_scheduler_remove_actor_is_lazy_and_correct():
    s = Scheduler()
    s.schedule("a", 1.0)
    s.schedule("b", 2.0)
    s.remove_actor("a")
    assert s.pop_due(10.0) == ["b"]


def test_run_until_empty_or_stopped_never_polls_faster_than_needed():
    """A single actor scheduled far in the future: the loop must sleep
    roughly that long in one call, not spin (item 30: no `while True: sleep(0.1)`)."""
    s = Scheduler()
    clock = SimClock(speed=1000.0)  # 1000x -- a "far future" sim event arrives soon in real time
    s.schedule("only", clock.now() + 5000.0)  # 5000 sim-seconds away = 5 real seconds at 1000x
    sleep_calls = []

    def fake_sleep(seconds):
        # Don't actually sleep in the test -- just record what was
        # requested. Real time barely advances, so the scheduled event
        # never actually becomes due in this test; should_stop() below
        # ends the loop once we've observed enough sleep requests.
        sleep_calls.append(seconds)

    def should_stop():
        return len(sleep_calls) >= 2

    acted = []

    def act(actor_id, now):
        acted.append(actor_id)
        return None

    run_until_empty_or_stopped(s, clock, act, should_stop, fake_sleep)
    # It should have requested a wait close to (but not exceeding) 5 real
    # seconds, capped at 5.0 by design -- never a 0.1s busy-poll value.
    assert any(4.0 <= c <= 5.0 for c in sleep_calls), sleep_calls


# --------------------------------------------------------------------------
# Clock (item 72): acceleration compresses pacing, not event density.
# --------------------------------------------------------------------------

def test_clock_speed_multiplies_simulated_time():
    clock = SimClock(speed=10.0)
    t0 = clock.now()
    time.sleep(0.05)
    t1 = clock.now()
    # 0.05 real seconds at 10x should be close to 0.5 simulated seconds --
    # loose bounds to absorb scheduling jitter in CI.
    assert 0.3 <= (t1 - t0) <= 1.5


def test_clock_resume_continues_from_persisted_point_not_a_fresh_origin():
    clock = SimClock(speed=5.0)
    snap = clock.snapshot_for_persist()
    resumed = SimClock.resume(snap)
    # Immediately after resume, simulated time must be very close to the
    # persisted value (paused during any real gap, never rewound/skipped).
    assert abs(resumed.now() - snap["origin_sim"]) < 1.0


# --------------------------------------------------------------------------
# Distributions (items 5-8): every value must be a real distribution draw,
# never a fixed constant, and every hard invariant must hold on every draw.
# --------------------------------------------------------------------------

def test_employee_profiles_are_a_real_probability_distribution():
    total_weight = sum(p.weight for p in distributions.EMPLOYEE_PROFILES)
    assert abs(total_weight - 1.0) < 1e-9
    rng = random.Random("profile-mix-check")
    picks = [distributions.pick_profile(rng).name for _ in range(2000)]
    # NORMAL_OPERATOR should dominate (item 5: 65%) but not be the only one.
    assert picks.count("NORMAL_OPERATOR") / len(picks) > 0.5
    assert len(set(picks)) == len(distributions.EMPLOYEE_PROFILES)  # every profile actually gets picked


def test_plan_session_duration_first_then_qty_derived():
    rng = random.Random("duration-first-check")
    profile = distributions.EMPLOYEE_PROFILES[0]
    durations_seen = set()
    for _ in range(200):
        qty, expected, actual = distributions.plan_session(rng, profile, standard_seconds_per_unit=60.0, qty_min=5, qty_max=100)
        assert qty >= 1
        assert expected == pytest.approx(60.0 * qty)
        assert actual > 0
        durations_seen.add(round(actual))
    # Not every session collapses to the same duration (was the original
    # bug's symptom: qty-first generation produced near-identical tiny durations).
    assert len(durations_seen) > 20


def test_good_defect_rework_invariants_always_hold():
    rng = random.Random("gdr-invariant-check")
    profile = distributions.EMPLOYEE_PROFILES[0]
    saw_zero_case = False
    for _ in range(3000):
        qty = rng.randint(0, 200)
        good, defect, rework = distributions.plan_good_defect_rework(rng, qty, profile)
        assert good >= 0 and defect >= 0 and rework >= 0
        assert rework <= defect  # item 8's hard invariant
        assert good + defect <= qty or qty == 0
        if qty > 0 and good == 0 and defect == 0:
            saw_zero_case = True
    assert saw_zero_case, "the valid GOOD=0/DEFECT=0 edge case (item 8) never occurred in 3000 draws"


def test_employee_starting_mid_shift_begins_soon_not_tomorrow():
    """Real bug found during live validation: a simulation bootstrapped
    while `now` already falls inside today's working window must have its
    employees start soon, not wait for tomorrow's shift start."""
    from datetime import datetime, timezone
    from engine.simulation.actors.employee import EmployeeActor, SHIFT_START_HOUR, SHIFT_END_HOUR
    from engine.simulation.factory_model import FactoryEmployee

    rng = random.Random("mid-shift-check")
    employee = FactoryEmployee(external_id=1, employee_no="QA-E001", name="Test", department="QC", qr="WF|EMP|QA-E001")
    actor = EmployeeActor(actor_id="emp:1", employee=employee, device_uuid="WEB-QA-SIM-KIOSK-1",
                           profile=distributions.EMPLOYEE_PROFILES[0], rng=rng, operations=[])

    mid_shift_hour = (SHIFT_START_HOUR + SHIFT_END_HOUR) / 2  # comfortably inside the working window
    dt = datetime.now(timezone.utc).replace(hour=int(mid_shift_hour), minute=0, second=0, microsecond=0)
    now = dt.timestamp()

    first_action = actor.initial_action_at(now)
    # Must be soon (within a few minutes), never pushed a full day ahead.
    assert now <= first_action < now + 3600, (first_action - now)


def test_heartbeat_and_web_read_pacing_is_not_compressed_by_speed():
    """Item 72: acceleration compresses employee session pacing, but
    heartbeat/dashboard-read cadence must stay a REAL-world interval --
    at speed=10, a KioskDeviceActor/WebUserActor's returned next_action_at
    must be ~10x further away in SIM time than at speed=1, so that once the
    scheduler divides back by speed to get a real sleep, the actual
    wall-clock gap is unchanged."""
    from engine.simulation.actors.kiosk_device import KioskDeviceActor, HEARTBEAT_INTERVAL_S

    class _FakeHeartbeatClient:
        def kiosk_heartbeat(self, device_uuid, status):
            return {"ok": True}

    rng1 = random.Random("hb-pacing-check")
    a1 = KioskDeviceActor(actor_id="kiosk:1", device_uuid="QA-SIM-KIOSK-1", rng=rng1)
    next_at_1x, _ = a1.act(1000.0, _FakeHeartbeatClient(), speed=1.0)
    delta_1x = next_at_1x - 1000.0

    rng2 = random.Random("hb-pacing-check")  # same seed -> same rng.uniform() draw
    a2 = KioskDeviceActor(actor_id="kiosk:1", device_uuid="QA-SIM-KIOSK-1", rng=rng2)
    next_at_10x, _ = a2.act(1000.0, _FakeHeartbeatClient(), speed=10.0)
    delta_10x = next_at_10x - 1000.0

    assert delta_10x == pytest.approx(delta_1x * 10, rel=1e-6)
    # And the underlying real-second interval itself must stay within the
    # documented bounds regardless of speed.
    assert HEARTBEAT_INTERVAL_S[0] <= delta_1x <= HEARTBEAT_INTERVAL_S[1]


def test_arrival_offsets_spread_around_shift_start():
    rng = random.Random("arrival-check")
    offsets = [distributions.pick_arrival_offset_minutes(rng) for _ in range(2000)]
    early = sum(1 for o in offsets if o < -2)
    on_time = sum(1 for o in offsets if -2 <= o <= 10)
    late = sum(1 for o in offsets if o > 10)
    assert early > 0 and on_time > 0 and late > 0  # item 12: not everyone starts simultaneously


# --------------------------------------------------------------------------
# Reconciliation (items 15/62/63) -- pure-logic checks against fake dashboard
# payloads shaped like the real /api/dashboard/shift response, no live HTTP.
# --------------------------------------------------------------------------

class _FakeClient:
    def __init__(self, payload):
        self._payload = payload

    def dashboard_shift(self, *, shift_date, shift_id):
        return self._payload


def test_dashboard_reconciliation_passes_when_sums_agree():
    from engine.simulation.reconciliation import check_dashboard_reconciliation
    payload = {
        "sessions": [
            {"operation_id": 1, "session_status": "CLOSED", "good_qty": 10, "defect_qty": 1},
            {"operation_id": 1, "session_status": "CLOSED", "good_qty": 5, "defect_qty": 0},
            {"operation_id": 1, "session_status": "OPEN", "good_qty": 999, "defect_qty": 999},  # must be excluded
        ],
        "items": [{"operation_id": 1, "operation_code": "OP-1", "day_good_qty": 15, "day_defect_qty": 1}],
    }
    result = check_dashboard_reconciliation(_FakeClient(payload), shift_date="2026-08-23", shift_id=1, run_id="test-run-ok")
    assert result["ok"] is True
    assert result["mismatches"] == 0


def test_dashboard_reconciliation_flags_and_records_a_real_mismatch():
    from engine import bug_store, qa_store
    from engine.simulation.reconciliation import check_dashboard_reconciliation
    qa_store.reset_for_tests(qa_store.Path("/tmp") / f"qa_sim_test_{random.randint(1, 10**9)}.sqlite3")
    payload = {
        "sessions": [{"operation_id": 42, "session_status": "CLOSED", "good_qty": 23, "defect_qty": 2}],
        "items": [{"operation_id": 42, "operation_code": "BEND-02", "day_good_qty": 21, "day_defect_qty": 2}],
    }
    result = check_dashboard_reconciliation(_FakeClient(payload), shift_date="2026-08-23", shift_id=1, run_id="test-run-mismatch")
    assert result["mismatches"] == 1
    bugs = bug_store.list_bugs(run_id="test-run-mismatch")
    assert len(bugs) == 1
    assert bugs[0]["type"] == "DASHBOARD_SESSION_RECONCILIATION_FAILED"
    assert "23" in bugs[0]["expected"] and "21" in bugs[0]["actual"]


def test_active_worker_reconciliation_flags_mismatch():
    from engine import qa_store
    from engine.simulation.reconciliation import check_active_worker_reconciliation
    qa_store.reset_for_tests(qa_store.Path("/tmp") / f"qa_sim_test_{random.randint(1, 10**9)}.sqlite3")
    payload = {"sessions": [{"employee_id": 1, "session_status": "OPEN"}]}
    result = check_active_worker_reconciliation(
        _FakeClient(payload), shift_date="2026-08-23", shift_id=1, run_id="test-run-worker",
        expected_open_employee_ids={1, 2},  # simulation thinks employee 2 is also OPEN -- dashboard disagrees
    )
    assert result["mismatched"] == 1
