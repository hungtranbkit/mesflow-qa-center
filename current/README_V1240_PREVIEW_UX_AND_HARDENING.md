# QA Center 1.24.0 — Preview Lab UX overhaul + hardening pass

A full pass over the v1.23.x Preview Lab / Regression Protection / Bug
Center screens: operator-facing UX, real data persistence, safer
networking, and a round of unit tests that were missing.

## UI Preview Lab UX (requirements 1/3/4/6)

- "Môi trường" renamed to **Môi trường Preview**, with a plain-language
  subtitle instead of Docker jargon, and an **ISOLATED PREVIEW** banner
  ("Không đọc hoặc ghi vào database MESFlow hiện tại.") at the top of the page.
- The raw `com.mesflow.qa.preview=1` label is gone from the main screen --
  it now lives inside a collapsible **Chi tiết kỹ thuật** section, alongside
  Network / App container / DB container / Image / Database hostname /
  Health endpoint.
- Single active preview at a time: once one exists, **Start Preview** is
  hidden until it's deleted (`Delete Environment` → `Start Preview`).
- Explicit phase copy instead of raw status enums: CREATING → "Đang tạo
  database và container riêng...", a new **SEEDING** phase → "Đang tạo dữ
  liệu Preview...", FAILED is shown as **ERROR** → "Không thể tạo môi trường
  Preview." with `[Xem lỗi]`/`[Delete Environment]`.
- Action buttons are status-gated, not always-enabled: READY gets
  `Open Preview / Run UI Coverage / Reset-Re-seed / Stop / Delete`; STOPPED
  gets `Start lại / Delete`; CREATING/STARTING/SEEDING expose no destructive
  action at all, only the page's own `Làm mới`.
- The status card shows Backend Port, and `Open Preview` now points at a
  server-computed `base_url` (see below) instead of a hardcoded
  `127.0.0.1` in the frontend.

## "Làm mới" is now provably read-only (requirement 2)

`GET /api/preview/environments` and `.../<id>` merge in a new
`PreviewManager.runtime_state()` -- live `docker inspect` status for the app
and db containers, plus a best-effort health probe -- **without calling
run/stop/start/rm/create anywhere**, and without writing to the database.
`test_refresh_endpoints_never_issue_a_mutating_docker_command` asserts this
against a fake Docker CLI that raises on anything but `inspect`.

## Named volume persistence (requirement 7)

Previously the preview's Postgres had no volume at all -- data lived only in
the db container's own writable layer, with no guarantee against future
recreate-on-error paths. Now:

- `finish_create()` creates `mesflow-ui-data-<id>`, labeled
  `com.mesflow.qa.preview=1` **and** `com.mesflow.qa.preview.id=<id>`, and
  mounts it at `/var/lib/postgresql/data`.
- `stop()`/`start()` only ever `docker stop`/`docker start` the existing
  containers -- never `run`, never touches the volume.
- `delete()` removes the volume last, guarded by
  `assert_preview_volume_label()`, which checks **both** labels -- a
  mismatched id label refuses the delete outright (defense in depth against
  a future bug in id/name construction ever deleting the wrong preview's
  data).

## Networking no longer depends on the published host port (requirement 6)

The original code waited for readiness (and later, coverage-ran the
browser) via `http://host.docker.internal:<port>` -- which only works if the
port is bound to `0.0.0.0`. That's what silently broke the very first real
preview creation in 1.23.2 (see the 1.23.3 note below already in this repo's
history) once the port was correctly defaulted to loopback-only.

Fixed properly instead of just widening the bind:
`internal_base_url()` now goes over the preview's own Docker network by
**container name** (`http://mesflow-ui-preview-<id>:8080`) -- QA Center is
already `connect_network()`'d onto it. Internal readiness/coverage checks
are now completely independent of the published port's bind host.

New env vars (old `MESFLOW_QA_PREVIEW_PORT_START/END` still honored):

- `MESFLOW_PREVIEW_BIND_HOST` (default `127.0.0.1`) -- host interface the
  published port binds to. Never `0.0.0.0` by default.
- `MESFLOW_PREVIEW_PUBLIC_HOST` (default = bind host, or `127.0.0.1` if bind
  host is `0.0.0.0`) -- what a human's browser should be told to open
  (`Open Preview`). Lets an operator reachable over LAN set this to that
  host's real IP without changing the bind.
- `MESFLOW_PREVIEW_PORT_START` / `_END` -- port range.

## Regression Protection / Bug Center (requirements 8-10, 15)

- `/api/regression/features` now includes `regression_cases` per feature
  (derived from bug_store, not duplicated storage -- see
  `regression_policy.regression_cases_for()`).
- `/api/regression/impact` returns `impacted_detail`: per-feature regression
  cases + coverage-gap flag, so the UI's new **Regression Impact** panel can
  show "N features affected" with a `NEEDS REGRESSION` tag and an actual
  reason per feature, not just a bare list of keys.
- Feature keys get a friendly display name client-side
  (`dashboard.production_overview` → "Dashboard Production Overview") while
  the real key stays the identifier everywhere else.
- Bug cards now surface MESFlow version / commit / preview preset / run id /
  regression testcase (pulled from the bug's latest evidence entry), plus
  legible console-error / page-error / backend-log sections instead of only
  a raw JSON dump. Added a Run ID filter.
- UI Coverage now captures a best-effort screenshot at the login and app
  pages and attaches it to matching findings (`evidence.screenshot_path`),
  served through a new, strictly path-validated
  `GET /api/preview/coverage/<run_id>/screenshot/<name>`.

## Navigation (requirement 17)

Every screen (Dashboard, UI Preview Lab, Regression Protection, Bugs) now
carries the same top nav strip linking all four, plus Demo Center.

**Scoped down from the full ask:** splitting the dashboard into separate
"Test Center" and "Release" pages was **not** done this round -- `index.html`
is asserted against by ~10 existing tests
(`test_ui_controls_cover_all_test_modes`,
`test_dashboard_has_cleanup_and_connection_controls`, etc.) and real
operator workflows; a page-per-concern split is a bigger, separate piece of
work than this pass's budget, and the risk of quietly breaking those flows
outweighed the navigation-purity gain. The nav bar delivers the same
"screens are separate, clearly reachable" outcome without the rewrite.

## Checked, found not applicable

- **Item 18** (rename a UI card that claims "Full UI Coverage" but actually
  runs an API test): no such card exists. The closest thing, "Factory
  Lifecycle" (`factory_simulation`), is already honestly named.
- **Item 19** (`agent.py` / `agent_legacy.py` split): there is no
  `agent_legacy.py` in this repo -- one 2400-line `agent.py`. Nothing to
  reconcile.
- **Item 22** (dedicated `/data/qa-center.db`): already satisfied by the
  existing `engine/qa_store.py` (`/data/state/qa_meta.sqlite3` by default,
  separate SQLite, never the MESFlow database, already survives restarts).
  Not renamed -- would risk data loss on an already-deployed volume for no
  functional gain.

## Tests

`tests/test_v1240_preview_ux_hardening.py` (new, 23 tests) covers: the named
volume's creation/mount/label-guarded deletion, stop/start never touching
containers or the volume, internal networking by container name, configurable
bind/public host, refresh-is-read-only (including a fake-docker allowlist
that fails the test if any mutating verb is issued), fingerprint
normalization, bug dedup/occurrences, SKIP_STABLE / CHANGED / coverage-gap
regression policy rules, the expected-state manifest validator, preview-DB
guards on wipe/seed, the coverage screenshot route's path validation, and a
copy-contract test locking in the new operator-facing wording.

Full suite: **169 passed**, including all pre-existing tests (Functional
Smoke / API Soak / Browser Visual / Behavioral / factory & realtime
simulation / release build+package / release gates / run history / logs /
kiosk -- nothing here required changing any of them).

## Bugs found by the real Docker smoke test (1.24.3)

A live end-to-end run (real Docker, real `mesflow-app`/Postgres images, no
mocks) surfaced three real bugs that no unit test could have caught, all
fixed in this version:

1. **`preview/seed.py`**: every preset except `EMPTY_STATE` crash-failed
   seeding with `column "plan_qty" of relation "operations" does not
   exist`. `operations` has no per-operation planned-quantity column --
   planned quantity lives only on `production_orders`. Fixed by dropping
   the column from the INSERT.
2. **`coverage_runner.py` login wait**: MESFlow's login page submits via
   `fetch()` + `location.href` (see `static/login.js`), not a normal
   `<form>` POST. `page.wait_for_load_state("domcontentloaded")` could
   return immediately against the pre-navigation page, before the async
   fetch resolved -- so a login that actually succeeded a moment later was
   still reported as `UNEXPECTED_LOGIN_REDIRECT`. Fixed with
   `page.wait_for_url(...)`, which waits for the URL itself to change.
3. **`coverage_runner.py` `_as_date()`**: assumed ISO 8601 dates;
   `/api/dashboard/production-orders` actually serializes `due_date` as an
   RFC 1123 HTTP-date (`"Thu, 20 Aug 2026 00:00:00 GMT"`). The ISO-only
   parser silently returned `None` for every real due date, so the "late
   production orders" `REPORT_MISMATCH` check could never fire even when
   the dashboard was genuinely undercounting. Fixed with
   `email.utils.parsedate_to_datetime`.

After all three fixes, a full real cycle passed: `EMPTY_STATE` create → READY
→ refresh (verified non-mutating via `updated_at` and container
`StartedAt` staying byte-identical across three refreshes) → delete → full
cleanup verified (no leftover container/network/volume). `FULL_UI` create →
READY → real UI Coverage run (Playwright against the real cloned app) →
2 genuine findings recorded as structured bugs with evidence and a real
screenshot → Stop → Start → **42 employees still present, byte-for-byte**
→ Delete → full cleanup verified. The real `mesflow-app`/`mesflow-postgres`
were confirmed untouched throughout (`docker ps`, row counts unaffected).

**Open question, not fixed:** the `FULL_UI` coverage run also flagged
"fewer open session exceptions visible than the seed guarantees" (expected
≥2, `/api/session-exceptions?view=inbox` returned 1). Direct inspection
shows `session_exception_reviews` genuinely has 7 seeded rows, but the live
"inbox" endpoint returns an exception computed from live session state with
its own fingerprint scheme (`OPEN_TOO_LONG:0`), unrelated to any fingerprint
`preview/seed.py` wrote. It's plausible "inbox" deliberately shows only
exceptions on still-*open* sessions (1 of the 7 seeded exceptions is on an
open session; the other 6 are `HIGH_DEFECT_RATE` on already-closed
sessions) -- which would make this a correct dashboard and an overly strict
coverage assumption, not a MESFlow bug. Left as-is rather than guess a fix
without reading the exception-detection code in `app/`; flagging for
someone who knows that subsystem.

## Known limitations (see final report for the complete list)

- "One active preview at a time" is UI-enforced only; the backend still
  technically accepts a second `POST /api/preview/environments` if called
  directly.
- "App/DB container ID" in Technical Details shows the container **name**
  (`mesflow-ui-preview-<id>`), not the raw Docker container hash -- that's
  the identifier used everywhere else in this system, and avoids an extra
  `docker inspect` per refresh for a rarely-needed value.
- Coverage screenshots cover only the login and app-dashboard pages, not
  every page a future, deeper UI Coverage pass might visit.
- A pre-existing, unrelated bug was found (not introduced by this pass):
  `agent.py`'s global `@app.errorhandler(Exception)` also catches Flask's
  own `HTTPException`s (e.g. a genuine 404 from an unmatched route), turning
  them into a 500. Left alone -- fixing a shared global handler in a
  2400-line file was out of scope for this pass and risks unrelated
  regressions; flagged here instead of silently working around it in tests.
