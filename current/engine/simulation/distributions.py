"""Realistic behavior distributions (spec items 5-8, 20, 47).

Every "how long / how many / how good" number an employee actor needs
comes from here, never a fixed constant -- same principle already
established in engine/preview/seed.py's REPORT_30_DAYS generator (duration
derived from quantity + a target productivity, not guessed independently),
reused here for the soak's continuous per-session behavior instead of a
30-day batch backfill.
"""
from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass(frozen=True)
class EmployeeProfile:
    name: str
    productivity_range: tuple[float, float]   # multiplier on standard time
    mistake_rate: float                        # item 20: low-rate realistic errors
    weight: float                              # selection probability


# Item 5's example distribution.
EMPLOYEE_PROFILES: tuple[EmployeeProfile, ...] = (
    EmployeeProfile("NORMAL_OPERATOR", (0.8, 1.2), 0.005, 0.65),
    EmployeeProfile("FAST_OPERATOR", (1.05, 1.5), 0.005, 0.10),
    EmployeeProfile("SLOW_OPERATOR", (0.5, 0.9), 0.008, 0.10),
    EmployeeProfile("NEW_OPERATOR", (0.45, 0.85), 0.02, 0.08),
    EmployeeProfile("ERROR_PRONE_OPERATOR", (0.6, 1.0), 0.05, 0.05),
    EmployeeProfile("MULTI_SKILL_OPERATOR", (0.9, 1.3), 0.005, 0.02),
)


def pick_profile(rng: random.Random) -> EmployeeProfile:
    return rng.choices(EMPLOYEE_PROFILES, weights=[p.weight for p in EMPLOYEE_PROFILES], k=1)[0]


# Item 6: session-length bands (minutes) an employee's planned batch size is
# sized to land in, before productivity variance is applied.
_SESSION_LENGTH_BANDS: tuple[tuple[float, float, float], ...] = (
    (5, 20, 0.15),     # short operation
    (20, 90, 0.55),    # normal
    (90, 240, 0.25),   # long
    (240, 600, 0.05),  # rare (4-10h)
)


def pick_target_minutes(rng: random.Random) -> float:
    r = rng.random()
    acc = 0.0
    for lo, hi, weight in _SESSION_LENGTH_BANDS:
        acc += weight
        if r <= acc:
            return rng.uniform(lo, hi)
    return rng.uniform(*_SESSION_LENGTH_BANDS[-1][:2])


def plan_session(rng: random.Random, profile: EmployeeProfile, standard_seconds_per_unit: float,
                  qty_min: int, qty_max: int) -> tuple[int, float, float]:
    """Item 6's own formula, applied per session:

        expected_seconds = standard_seconds_per_unit * units
        actual_seconds    = expected_seconds / productivity_factor

    Target duration is chosen first (a realistic minute band), quantity is
    CALCULATED from it (same duration-first inversion already validated for
    REPORT_30_DAYS), then softly bounded to the operation's realistic qty
    range and re-derived so the final numbers stay mutually consistent.
    Returns (qty, expected_seconds, actual_seconds)."""
    productivity = rng.uniform(*profile.productivity_range)
    target_seconds = pick_target_minutes(rng) * 60.0
    expected = target_seconds * productivity
    qty = max(1, round(expected / standard_seconds_per_unit)) if standard_seconds_per_unit > 0 else 1
    qty = max(max(1, qty_min // 2), min(qty_max * 2, qty))
    expected = standard_seconds_per_unit * qty
    actual = expected / productivity if productivity > 0 else expected
    return qty, expected, actual


def plan_good_defect_rework(rng: random.Random, qty: int, profile: EmployeeProfile) -> tuple[int, int, int]:
    """Item 8's baseline (GOOD 90-99.5%, DEFECT 0.5-10%, REWORK <= DEFECT),
    widened slightly for ERROR_PRONE/NEW profiles, always enforcing
    REWORK <= DEFECT and allowing the valid GOOD=0/DEFECT=0 edge case
    (item 8: "zero-quantity completed sessions are valid enough to exist
    and can later produce ZERO_QTY_LONG exceptions")."""
    if qty <= 0:
        return 0, 0, 0
    if rng.random() < 0.01:  # rare valid zero-output session
        return 0, 0, 0
    defect_rate_hi = 0.10 if profile.name in ("ERROR_PRONE_OPERATOR", "NEW_OPERATOR") else 0.06
    defect_rate = rng.uniform(0.005, defect_rate_hi)
    defect = round(qty * defect_rate)
    good = max(0, qty - defect)
    rework = round(defect * rng.uniform(0.0, 0.8)) if defect else 0
    rework = min(rework, defect)
    return good, defect, rework


# Item 4/71: factory population baselines.
FACTORY_PROFILES: dict[str, dict[str, int]] = {
    "SMALL_FACTORY": {"employees": 15, "supervisors": 2, "managers": 1, "kiosks": 2, "active_pos": 3},
    "MEDIUM_FACTORY": {"employees": 40, "supervisors": 3, "managers": 2, "kiosks": 6, "active_pos": 8},
    "LARGE_FACTORY": {"employees": 100, "supervisors": 5, "managers": 3, "kiosks": 15, "active_pos": 20},
}

# Item 12: arrival-time jitter around shift start, in minutes (negative =
# early). Weighted bands matching the spec's own example distribution.
_ARRIVAL_BANDS_MIN: tuple[tuple[float, float, float], ...] = (
    (-15, -5, 0.10), (-5, 10, 0.60), (10, 30, 0.20), (30, 60, 0.10),
)


def pick_arrival_offset_minutes(rng: random.Random) -> float:
    r = rng.random()
    acc = 0.0
    for lo, hi, weight in _ARRIVAL_BANDS_MIN:
        acc += weight
        if r <= acc:
            return rng.uniform(lo, hi)
    return rng.uniform(*_ARRIVAL_BANDS_MIN[-1][:2])
