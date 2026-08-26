# QA Center 1.27.0 — REPORT_30_DAYS: dense, non-overlapping productivity history

Scoped change: **seed/import logic only** (`engine/preview/seed.py` +
`engine/preview/expectations.py`), per the task's own instruction. No
backend defect was found this round, so `app/`, `agent.py`,
`preview_manager.py`, and `coverage_runner.py` are untouched.

## What changed

`_seed_report_30_days`'s CLOSED-session generation was rewritten from
"per-operation chunks assigned round-robin to employees" to a proper
**per-employee schedule**:

1. **Allocation** — decide how many sessions ("slots") each real operation
   supplies, proportional to its own `done_qty` share of a target total
   (`employee_targets` summed); split that operation's `done_qty`/
   `defect_qty` across exactly that many slots (`_split_into_n_chunks`, new).
   This keeps the existing invariant: an operation's aggregate is always
   the exact sum of its own sessions.
2. **Distribution** — shuffle all slots together (deterministic), hand each
   employee exactly their own target count (25 values, each `randint(30,45)`,
   sum naturally in `[750, 1125]`).
3. **Scheduling** — each employee's sessions are laid out chronologically
   (oldest day first) across 18-27 active days out of the last 30 (some
   days doubled up, some skipped), most starting in a 6:00-15:00 window.
   A **per-employee time cursor only ever moves forward**
   (`started = max(desired_start, cursor + 20min)`), so the same employee
   can never have two sessions overlap — verified with zero violations
   against real inserted rows (window-function self-check, see below).
4. **Productivity**: `_productivity_pct_for` now **hard-clamps** to
   `[50, 150]` (was a soft floor of 40, no ceiling) — every single
   session's target lands in range by construction, not "usually".

## Verified against the real MESFlow schema (not just unit tests)

pg_dump'd from the live `mesflow-postgres`, loaded into a throwaway
Postgres, ran the real seed:

| Check | Result |
|---|---|
| employees | 25 |
| closed sessions | 924 (target 750-1125) |
| min sessions / employee | 30 (all 25 employees land in [30,45]: 30,31,32,33,34,35,36×4,37,38×3,39,40,41,43,44×2,45) |
| open sessions | 8 (target 6-10) |
| productivity-valid sessions | 920/924 = 99.6% (target ≥95%) |
| avg productivity (session-level mean) | 91.43% (target >80, "preferably 85-105") |
| min / max session productivity | 50.00% / 131.60% (target ≥~50 / ≤~150) |
| **overlap violations (window-function self-join)** | **0** |
| operation done_qty/defect_qty vs. SUM(its sessions) | **0 mismatches** across all 30 normal operations |
| seed wall-clock time (924 sessions) | 0.3s |

## Tests

Extended `tests/test_v1260_report_30_days_productivity.py`: upgraded the
shared `_FakeCursor` to actually record every `work_sessions` INSERT
(employee/started_at/ended_at), enabling two new tests that need no real
Postgres:

- `test_report_30_days_full_assertion_list` — the exact assertion list from
  the task (employee count, closed-session count, min-per-employee,
  valid-session ratio, avg productivity, min/max productivity).
- `test_report_30_days_employee_sessions_never_overlap` — sorts each
  employee's own sessions by `started_at` and asserts every session starts
  at or after the previous one's `ended_at`.
- `test_report_30_days_working_hours_and_days_off` — most sessions land in
  a normal-hours window; at least one employee has a day with 2+ sessions
  and at least one employee has a rest day.

Two pre-existing tests in that file were updated for the new targets
(750-1125 instead of 250-400) rather than left silently stale, and the
duration-formula test now checks the real 3-tuple return
(`session_id, target_pct, ended_at`) instead of the old 2-tuple.

Full suite: **209 passed**, zero regressions in anything untouched this
round (FULL_UI, the shared preview lifecycle, Bug Center, Regression
Protection all unaffected).

## Remaining limitations

- Operation `planned_quantity`/fraction ranges were widened (280-520 vs.
  180-360) purely to supply enough aggregate `done_qty` for ~900 sessions
  at a realistic per-session batch size (~8-12 units) — a side effect of
  the density increase, not an independent requirement.
- The per-employee "6:00-15:00 desired start" can still drift outside
  normal hours when the no-overlap cursor pushes a session later (e.g. an
  unusually long prior session spills into the evening) — by design
  ("avoid impossible overlap" takes priority over "stay in-hours" when
  they conflict); confirmed ≥80% of sessions land in a 6:00-20:00 window.
- Not yet deployed — `1.27.0` is built and frozen; deploy via Deploy Agent
  → QA Release Manager → Deploy Local to see it live.
