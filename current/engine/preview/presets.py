"""Dataset presets for the UI Preview Lab (requirement 1).

Every count here exists for a reason -- there is no random noise. Each
preset's docstring says what a reviewer should see when they open the
preview and why.
"""
from __future__ import annotations

from dataclasses import dataclass, field

PRESETS = (
    "FULL_UI",
    "NORMAL_FACTORY",
    "PROBLEM_FACTORY",
    "REPORT_30_DAYS",
    "EMPTY_STATE",
    "EDGE_CASES",
)


class UnknownPresetError(ValueError):
    pass


def validate(preset: str) -> None:
    if preset not in PRESETS:
        raise UnknownPresetError(f"Unknown preset {preset!r}, expected one of {PRESETS}")


@dataclass
class PresetSpec:
    key: str
    description: str
    employees: int = 12
    stations: int = 4
    pos_late: int = 0          # due_date = now - 2 days, still IN_PROGRESS
    pos_warning: int = 0       # due_date = now + 6 hours, still IN_PROGRESS
    pos_completed: int = 0     # status COMPLETED, spread across last 30 days
    pos_on_track: int = 0      # IN_PROGRESS, due_date comfortably in the future
    sessions_open_normal: int = 0   # started_at = now - 75 minutes, status OPEN
    sessions_long_open: int = 0     # started_at = now - 26 hours, status OPEN
    sessions_closed_history: int = 0  # CLOSED, spread across the last 30 days
    exceptions_minimum: int = 0      # session_exception_reviews, status NEW
    edge_cases: bool = False
    # Factory-scale realistic generation (currently FULL_UI only -- see
    # seed.py's _seed_realistic_operations()). When True, employees/PO
    # status counts above are ignored in favor of _FULL_UI_OPERATION_PLAN:
    # many operations deliberately spread across every completion-%% band,
    # each with real multi-session work history, so Dashboard/Reports/
    # Operation-list/Kiosk all have production-scale data to show instead
    # of a handful of toy rows.
    realistic_scale: bool = False
    # Productivity-report scale (REPORT_30_DAYS only -- see seed.py's
    # _seed_report_30_days()). When True, realistic_scale/employees/PO
    # status counts above are ignored in favor of a 25-employee, 30-day
    # factory dataset built specifically to exercise the employee
    # productivity/efficiency report: real operation types with a fixed
    # standard_seconds_per_unit each, and every session's duration derived
    # from its employee's own baseline productivity -- never a guessed
    # duration next to an independently-guessed quantity.
    productivity_scale: bool = False


SPECS: dict[str, PresetSpec] = {
    "FULL_UI": PresetSpec(
        key="FULL_UI",
        description=(
            "Factory-scale mix: 25 employees, ~20 operations deliberately spread "
            "across every completion-%% band (0/10-20/30-50/60-80/90-99/100/>100), "
            "150-250 real multi-session work histories, 6-12 active sessions -- "
            "looks like a real running factory, not a handful of demo rows."
        ),
        employees=25, stations=6,
        exceptions_minimum=2,
        realistic_scale=True,
    ),
    "NORMAL_FACTORY": PresetSpec(
        key="NORMAL_FACTORY",
        description="Everything nominal: on-time POs, normal sessions, no exceptions. The 'quiet day' baseline.",
        employees=14, stations=4,
        pos_late=0, pos_warning=0, pos_completed=2, pos_on_track=3,
        sessions_open_normal=3, sessions_long_open=0, sessions_closed_history=10,
        exceptions_minimum=0,
    ),
    "PROBLEM_FACTORY": PresetSpec(
        key="PROBLEM_FACTORY",
        description="Heavy on trouble: late/warning POs, long-open sessions, multiple exceptions.",
        employees=14, stations=4,
        pos_late=4, pos_warning=3, pos_completed=1, pos_on_track=1,
        sessions_open_normal=2, sessions_long_open=3, sessions_closed_history=8,
        exceptions_minimum=5,
    ),
    "REPORT_30_DAYS": PresetSpec(
        key="REPORT_30_DAYS",
        description=(
            "Productivity-report scale: 25 employees, 9 real operation types "
            "(each with a realistic standard_seconds_per_unit and its own "
            "realistic units/session range), 12 production orders / ~30 "
            "operations, 750-1125 CLOSED sessions (30-45/employee) with "
            "realistic durations (median ~90-150 min) driven by each "
            "employee's own worked-hours target (~70-150h/30 days) and "
            "baseline productivity, 6-10 OPEN sessions -- the standard "
            "dataset for testing the employee productivity report."
        ),
        employees=25, stations=8,
        exceptions_minimum=1,
        productivity_scale=True,
    ),
    "EMPTY_STATE": PresetSpec(
        key="EMPTY_STATE",
        description="Nothing beyond master data (employees/stations). Tests blank-page rendering.",
        employees=2, stations=1,
        pos_late=0, pos_warning=0, pos_completed=0, pos_on_track=0,
        sessions_open_normal=0, sessions_long_open=0, sessions_closed_history=0,
        exceptions_minimum=0,
    ),
    "EDGE_CASES": PresetSpec(
        key="EDGE_CASES",
        description="Boundary values: zero-quantity PO, due date exactly now, unicode/long names, 0/0 session.",
        employees=6, stations=2,
        pos_late=1, pos_warning=1, pos_completed=1, pos_on_track=1,
        sessions_open_normal=1, sessions_long_open=1, sessions_closed_history=3,
        exceptions_minimum=2,
        edge_cases=True,
    ),
}


def get_spec(preset: str) -> PresetSpec:
    validate(preset)
    return SPECS[preset]
