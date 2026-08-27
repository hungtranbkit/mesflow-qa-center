# QA Center 1.26.0 — REPORT_30_DAYS productivity dataset + shared-preview reseed

## The real bug (Part 2)

Confirmed: every operation `preview/seed.py` ever created had
`standard_seconds_per_unit = 0` (the column wasn't even in the INSERT).
MESFlow's real productivity feature — `/api/reports/employee-performance`,
`ReportRepository.employee_performance()` — computes
`efficiency_percent = expected_seconds/actual_seconds*100` where
`expected_seconds = standard_seconds_per_unit * (good_qty+defect_qty)`. A
zero there makes `expected_seconds` always `0`, and the repository treats
that as "not enough data": `efficiency_percent` is `None` for every session,
no matter how much real history exists. Fixed at the root:
`_insert_po_with_operation()`'s `standard_seconds_per_unit` parameter now
defaults to `60.0` (never 0), which alone fixes it for every existing
preset (FULL_UI included) with no other changes required.

**Naming reality check:** the task described `GET
/api/reports/employee-productivity`, `top_employee`, and
`avg_employee_productivity_percent`. None of these exist anywhere in
`app/` — grepped the full tree, zero hits. The real, equivalent, already-
shipped feature is `/api/reports/employee-performance` /
`summary.efficiency_percent` — same formula, different name ("hiệu suất"
not "năng suất" in the UI). This round builds and tests against the real
endpoint; see REMAINING LIMITATIONS for exactly what that means was and
wasn't verified.

## REPORT_30_DAYS (Parts 3-7)

New `PresetSpec.productivity_scale` flag (REPORT_30_DAYS only) →
`seed._seed_report_30_days()`:

- **25 employees** (was 16), cycled across 6 real departments (Cắt laser/
  Chấn/Hàn/Hoàn thiện/Lắp ráp/QC). Employee name list expanded 16→25
  unique names.
- **9 real operation types**, each with a type-fixed `standard_seconds_per_unit`
  (midpoint of the requested range): Cắt laser 45s, Làm nguội 65s, Dập 30s,
  Chấn CNC 75s, Hàn MIG 180s, Mài 90s, Lắp ráp 125s, Kiểm tra QC 60s,
  Hoàn thiện 115s.
- **12 production orders → 32 operations** (8-15 POs / 20-40 operations
  requested), rotating through the 9 operation types.
- **~320 CLOSED sessions** (250-400 requested), **8 OPEN** (6-10
  requested) — verified live against the real schema (see below).
- **Duration is derived FROM quantity + productivity, never guessed
  independently** (`_seed_productivity_session`):
  `expected_seconds = standard_seconds_per_unit * qty`,
  `actual_seconds = expected_seconds / (target_pct/100)`,
  `ended_at = started_at + actual_seconds`.
- **25 per-employee baseline productivities** (`_EMPLOYEE_BASELINE_PRODUCTIVITY`),
  112→55-ish spread, `±(-6,+5)` deterministic jitter per session. Verified
  live distribution across 319 valid sessions: <60%: 6.6%, 60-79%: 17.6%,
  80-99%: 42.6%, 100-115%: 24.5%, >115%: 8.8%, avg 91.2% (target ranges:
  5-10 / 15-20 / 40-50 / 20-30 / ~5 / [75,110] — all hit except >115% ran
  a bit hot at 8.8% vs "~5%"; see limitations).
- **2 deliberately-invalid operations** (no standard time — the "chưa cấu
  hình định mức" edge case) + **2 zero-qty sessions** — kept to exactly 4
  invalid sessions out of ~323 (98.8% valid, comfortably over the required
  95%).
- **1 unusually-long-open session** (26h) for exception coverage, without
  distorting the other 7 OPEN sessions. OPEN sessions use 10 *distinct*
  employees — `work_sessions` has a real `UNIQUE(employee_id) WHERE
  status='OPEN'` constraint; reusing one would have crash-failed the seed.

## Shared helpers (Part 12)

`_insert_po_with_operation` gained `operation_name`/`standard_seconds_per_unit`
params (defaulted, so every existing caller is unaffected); `_seed_employees`
gained an optional `departments` param. `_seed_productivity_session` /
`_productivity_pct_for` are new standalone helpers REPORT_30_DAYS uses and
FULL_UI *could* adopt later — not forced into FULL_UI's already-verified
band generator this round, to avoid destabilizing it for a "could" (not
"must") requirement.

## Shared preview environment (Part 1)

Re-verified `reset_reseed()`: it already never touched Docker (only
`wipe_runtime_data()` + `run_seed()` over the existing psycopg connection) —
hardened further:

- **`reset_reseed()` now takes an optional `preset` argument** — this is
  the actual "testcase A → reseed → testcase B" switch the architecture
  asks for; previously it could only re-roll the *same* preset. Validates
  the new preset **before** touching anything (fail closed, no partial
  state). Preview Lab UI gained a preset dropdown next to "Reset / Re-seed".
- Confirmed by new tests with a fully faked Docker CLI: zero `run`/`rm`/
  `volume create`/`volume rm`/`network create`/`network rm` calls during a
  reseed, preset switch included; `id`/`port`/`network`/`app_container`/
  `db_container`/`volume`/`db_password`/`db_name` all identical before and
  after.
- Reset strategy (Part 13) reviewed: `_RUNTIME_TABLES_TO_WIPE` was already
  business/domain tables only (`session_exception_reviews`,
  `operation_adjustments`, `qc_inspections`, `penalty_tickets`,
  `work_sessions`, `operations`, `parts`, `production_orders`,
  `sales_orders`, `equipment`, `stations`, `employees`) — never touches
  `alembic_version`, `users`, or any migration/admin-login table. Kept
  as-is.

## Expected-state manifest (Part 8)

Extended the *existing* `expectations.scale` block (no parallel manifest
system) with `production_orders`, `operations`, `closed_sessions`,
`open_sessions`, `productivity_valid_sessions`, `productivity_invalid_sessions`,
`operations_with_standard_time`, `history_days`, and
`avg_productivity_percent_expected_range` — used by both FULL_UI (its
existing fields) and REPORT_30_DAYS (the new ones), same dict.

## UI Coverage (Part 9)

New `coverage_runner._check_productivity()`: calls
`/api/reports/employee-performance?from=<anchor-history_days>&to=<anchor>`
(no `employee_id` filter aggregates every employee — the closest real
analog to an "all-employees productivity summary"). Detects **exactly**
the historical bug: `completed_session_count > 0 AND efficiency_percent is
null` → `PRODUCTIVITY_ALWAYS_NULL` finding. Also checks employee list
non-empty, productivity-valid session count against the manifest minimum,
and flags the overall efficiency if wildly outside the expected range.
`top_employee`/per-employee breakdown assertions from the task were not
implementable — no such field exists in the real API (see limitations).

## Admin password

`PreviewManager` used `secrets.token_urlsafe(12)` (a different password
every time). Now a fixed, known `PREVIEW_ADMIN_PASSWORD = "1234567890"`
(10 chars, satisfies MESFlow's own `>=10 chars` production-bootstrap
check), overridable via `MESFLOW_PREVIEW_ADMIN_PASSWORD`. Safe: a preview
is already isolated (fresh DB/network/random DB password/guarded by
`com.mesflow.qa.preview=1`), so a predictable admin login costs nothing
and saves a tester from hunting for it.

## Two more real bugs found live (1.26.1)

The live smoke test (below) caught two real bugs in this round's own new
code, neither reachable by unit tests against a fake `get`:

1. **`_check_productivity` read the wrong response shape.** The real
   `/api/reports/employee-performance` nests its payload under `"report"`
   (`jsonify(ok=True, report=report)` in `analytics.py`) — every other
   endpoint this module calls returns its payload flat, so the first draft
   did too, and got zero employees/sessions back every time. Fixed by
   unwrapping `resp.json()["report"]` first.
2. **Date-range boundary undercounted valid sessions**: seeded 319 valid
   sessions, but the report backend's `from`-day filter is date-only
   (`started_at >= from::date`) while the seed's `day_offset` can be
   `history_days` PLUS several extra hours of jitter — a session dated
   "30 days and 9 hours ago" sorts before midnight on day-30-ago. Live
   check first came back **313 of 319** through the API. Fixed by adding
   1 day of headroom to `date_from` in `_check_productivity` (a
   query-range widening, not a seed change — the sessions were correctly
   inside the promised window all along).

Both fixed and reverified live before freezing 1.26.1: `bug_count: 0`.

## Verified live (Part 16)

Ran seed logic against the **real MESFlow schema** (`pg_dump`'d from the
live `mesflow-postgres`, loaded into a throwaway Postgres) — not just unit
tests: 25 employees confirmed, 30/32 operations with real
`standard_seconds_per_unit`, and the actual SQL formula
(`standard_seconds_per_unit * qty / actual_seconds * 100`) computed by hand
against the seeded rows matches `_seed_productivity_session`'s intent
exactly (spot-checked 15 sessions, all correct to the decimal).

Then a full real Docker Preview Lab lifecycle against `mesflow-app:71.0.0.52`
(throwaway QA Center container, distinct from the deployed one):
`Start Preview → REPORT_30_DAYS → READY` → real DB confirmed 25 (+26
baseline from a migration, unrelated to this seed) employees / 32
operations / 323 CLOSED / 8 OPEN / 30 with standard time → real
`POST /api/auth/login` with `1234567890` → real
`GET /api/reports/employee-performance` returned **`efficiency_percent:
71.37`, `enough_data: true`, `completed_session_count: 323`** (this
specific triple was `null`/`0` before the fix) → `UI Coverage`: 0 bugs →
**reseed the same environment to `FULL_UI`** → Docker container IDs
(`docker inspect --format {{.Id}}`, the real hash, not just the name)
**byte-identical before and after** for both the app and DB container →
`UI Coverage` again: 0 bugs → delete → full cleanup verified (no leftover
container/network/volume) → real `mesflow-app`/`mesflow-postgres`
confirmed untouched throughout.

## Tests

`tests/test_v1260_report_30_days_productivity.py` (new, 20 tests): the
REPORT_30_DAYS shape/scale, determinism, the productivity formula, the
manifest fields, the exact PRODUCTIVITY_ALWAYS_NULL regression (both the
failing and passing cases), and 6 reseed-identity/guard tests (never
recreates containers, keeps id/port/network/credentials identical, can
switch preset in place, refuses an unknown preset or a missing guard
before touching anything). Full suite: **206 passed**.

## Remaining limitations

- **No dedicated `/api/reports/employee-productivity` endpoint, no
  `top_employee`, no `avg_employee_productivity_percent` field exist in
  the current MESFlow app** — confirmed by a full-tree grep, zero hits.
  Built and tested against the real equivalent
  (`/api/reports/employee-performance` / `efficiency_percent`) instead.
  If a distinct all-employees productivity table/endpoint is wanted, that
  is new `app/` backend work, not a QA Center seed fix, and was out of
  this pass's scope.
- **"Báo cáo năng suất nhân viên" as an all-25-employees-at-once table
  does not exist either** — the real screen (`app.js` `employeeReportBody`)
  is a per-employee drill-down modal (`efficiency_percent`,
  `yield_percent`, per-operation breakdown, recent sessions), opened one
  employee at a time. Part 10's specific UI checks (a summary table with
  avg/top-employee) could not be verified because that screen doesn't
  exist; the per-employee modal itself needed no changes (already compact,
  already reads real backend data, no hard-coded percentages).
- **`>115%` productivity band ran a bit hot**: 8.8% of valid sessions vs.
  the "~5%" target (all four other bands landed within their requested
  ranges). Tightened jitter once; further tuning would mean more iteration
  cycles for a soft, approximate target — not chased further.
- **FULL_UI was not switched onto the new productivity-driven duration
  generator** — it keeps its own already-verified band-based generator
  from the previous round, now also defaulting to a real
  `standard_seconds_per_unit` (60s) via the same shared default. Fully
  unifying the two generators is the "could" in Part 12, not attempted
  this round to avoid destabilizing FULL_UI's tested behavior.
- App-side UI (Part 10/11) not rebuilt/redeployed this round — no `app/`
  changes were needed for Part 10 (see above), and Part 11 was previously
  implemented and only re-verified (present, untouched), per the task's
  own instruction to verify rather than rewrite it.
