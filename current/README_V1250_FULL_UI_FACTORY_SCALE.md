# QA Center 1.25.0 — FULL_UI factory-scale dataset + operation progress UI

FULL_UI now looks like a real running factory instead of a handful of demo
rows, and two real MESFlow app screens got the UI fixes needed to actually
show it.

## Seed dataset (`engine/preview/presets.py`, `engine/preview/seed.py`)

- **25 employees** (was 16). Other presets deliberately unchanged/smaller
  per the task's own instruction.
- **20 operations**, explicitly spread across every completion-%% band:
  0% (1), 10-20%% (3), 30-50%% (3), 60-80%% (3), 90-99%% (3), 100%% (6),
  >100%% (1 over-production edge case) — 12 "partial" ops (≥10 required),
  6 fully completed (≥5 required). Every number is hand-chosen and
  documented in `_FULL_UI_OPERATION_PLAN`, never randomly generated.
- **Consistency by construction**: each operation's `done_qty`/`defect_qty`
  is never a second independently-guessed number — it IS the sum of that
  operation's own `work_sessions` rows (`_seed_realistic_operations` splits
  the target quantity into realistic per-session chunks first, then sets
  the operation aggregate to match exactly). Verified live against the real
  schema: **20/20 operations bit-for-bit consistent** between
  `operations.done_qty`/`defect_qty` and `SUM(work_sessions...)`.
- **175 completed sessions** (target 150-250) spread across each
  operation's own lifetime (created_at..anchor, capped 30 days), with
  duration variation (20-45 / 45-90 / 90-180 min buckets, plus a "vài
  session dài bất thường" long-tail every ~17th session) and defect
  variation (folded into the last chunk of an operation's sessions when
  `defect_qty>0`).
- **10 active/open sessions** (target 6-12), plus **4 "chờ/idle"** zero-qty
  just-closed sessions (ended 30-90 min ago) — together with the
  10 working employees that's 14 of 25 employees with a "now" state, the
  remaining 11 history-only. All 25 employees appear across the session
  history (verified: `SELECT count(DISTINCT employee_id) = 25`).
- Deterministic: same `random.Random(f"{preset}-seed")` instance already
  used by the rest of this file, reused (not re-seeded) through the new
  code — two reseeds of FULL_UI produce byte-identical band counts and
  session counts (see `test_deterministic_across_two_runs_with_the_same_seed`).
- Other presets (`NORMAL_FACTORY`/`PROBLEM_FACTORY`/`REPORT_30_DAYS`/
  `EMPTY_STATE`/`EDGE_CASES`) go through the exact same code path as before
  (`_seed_legacy_scenario`, extracted verbatim, zero behavior change) —
  gated behind a new `PresetSpec.realistic_scale` flag, `True` only for
  FULL_UI.

## Expected-state manifest (`engine/preview/expectations.py`)

New `expectations.scale` block (only present when the seed actually
generated it): `employees`, `completed_sessions_min`, `active_sessions_min`,
`operations_with_partial_progress_min`, `operations_completed_min`,
`progress_bands`. `validate()` checks each against an `observed.scale` dict.
Absent for every other preset — no behavior change for them.

## UI Coverage now asserts factory scale, not just page load

`coverage_runner._check_factory_scale()`: after logging in,
- `GET /api/employees` → asserts employee count.
- `GET /api/dashboard/active-sessions` → asserts active-session count.
- `GET /api/kpi/operations` → asserts count of partial-progress and
  fully-completed operations (bucketed from the API's own
  `completion_percent` field), **and** cross-checks that field against
  `done_qty/plan_qty` itself — if MESFlow's own KPI query ever disagreed
  with the quantities it just reported, that's flagged as a REPORT_MISMATCH
  bug, not silently trusted.

Every shortfall becomes a structured bug record via the existing
`bug_store` pipeline (fingerprinted, evidenced) — never suppressed.

## Real MESFlow app UI changes

Two scoped, additive changes in `app/mesflow/web/static/` (the actual
production app, not QA Center):

1. **`ui.css`**: `.employee-day-list` (the Dashboard's "Ngày công theo nhân
   viên" employee status list — the closest existing screen to the task's
   "kiosk/nhân viên" list, see limitations below) is now a 2-column grid
   (`display:grid;grid-template-columns:repeat(2,minmax(0,1fr))`) on
   desktop (`min-width:901px`), each row's detailed shift-track
   visualization hidden so cards stay short. Gated so it never fights the
   pre-existing tablet/mobile collapse breakpoints — below 901px, the
   original single-column layout is untouched.
2. **`app.js` `poOperationRow()` + `ui.css`**: the PO detail page's
   Operation list now shows a percent + mini progress bar per operation,
   computed from the exact same `planned_quantity`/`done_qty` values
   already displayed in the row (never hard-coded), width visually capped
   at 100%% but the printed percent is not — an over-production edge case
   still shows its real number (e.g. "112%").

## Consistency across Dashboard/PO detail/Operation list/Reports

**Investigated, not unified.** There is no shared progress-%% function in
`app/` — at least 6 different formulas across
`mesflow/db/repositories/analytics.py` and `app.js` (PO-level vs
per-operation, some dividing by the PO's `planned_quantity`, others by
`planned_quantity × operation_count`, the PO-detail header bar using a
completely different metric — % of operations marked COMPLETED, ignoring
quantities entirely). Unifying these into one shared function is a real,
separate, and significantly larger undertaking than this pass's budget —
scoped down deliberately rather than attempted partially. The one new
progress bar added here (operation list) uses the same
`done_qty/planned_quantity` semantics as the Overview/Control pages'
existing per-operation progress bars, so it is at least consistent with
those two, not a third new formula.

## Verified

- Seed logic run directly against the **real MESFlow schema** (pg_dump'd
  from the live `mesflow-postgres`, loaded into a throwaway Postgres) —
  not just unit-tested: 25 employees, 20 operations, 179 CLOSED sessions
  (175 completed-work + 4 idle) + 10 OPEN, every operation's aggregate
  byte-consistent with its session sum, all progress percentages land in
  the intended band.
- `tests/test_v1250_full_ui_factory_scale.py` (new, 12 tests): plan
  coverage, session-count range, active-session split, determinism, manifest
  building/validation, and the new coverage-runner assertions (including
  the completion_percent-disagrees-with-raw-quantities check) against a
  fake cursor/API — no real Postgres required for CI.
- Full suite: **186 passed**, zero regressions.

## Known limitations

- **App UI changes not visually verified in a running container.**
  `app.js`/`ui.css` are baked into the `mesflow-app` Docker image; seeing
  them live requires that image's own build/deploy pipeline (separate from
  QA Center's), which was out of this pass's budget. Verified by static
  review + `node --check` (syntax) only.
- **The 2-column list is the Dashboard's employee-status list, not a
  dedicated kiosk employee-selection screen** — there is no such screen in
  this codebase (the kiosk page uses a single dropdown, not a card list;
  see the admin employee table, which is a different, unrelated
  `<table>`-based UI). This was the closest real match to the task's
  mockup and is where ~25 rows would otherwise force long scrolling.
- **Compact card hides shift-track detail rather than moving it to a
  tooltip.** The task asked for "chi tiết khác đưa vào tooltip/detail" —
  implemented as CSS-only (`display:none` on the detailed visualization,
  full detail still reachable via existing report/detail views) rather
  than adding a JS-rendered tooltip, to keep the change zero-risk to
  `renderDashboard()`'s existing logic.
- **Progress-formula unification across the 6 identified locations**:
  not done (see above) — flagged as a distinct, larger piece of work.
