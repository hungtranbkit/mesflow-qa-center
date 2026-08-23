# UI Preview Lab

UI Preview Lab uses one isolated MESFlow backend + PostgreSQL database per preview environment. Test cases must reuse that environment instead of cloning a new backend/database for every UI case.

## Lifecycle

1. Start Preview: create isolated network, named DB volume, PostgreSQL, and MESFlow app container once.
2. Reset / Reseed: keep the same backend/database containers, wipe only preview business data, then seed the requested deterministic dataset.
3. Run UI Coverage: execute one or many UI cases against the same preview environment.
4. Stop / Resume: stop or resume the same environment while preserving the named DB volume.
5. Delete Environment: remove only resources owned by the preview id and labels.

A test case must never create a second MESFlow backend or PostgreSQL instance when a READY preview already exists. State isolation between cases is achieved through deterministic database reset/reseed scripts.

## Dataset policy

`REPORT_30_DAYS` is the canonical employee productivity dataset and must model a realistic 30-day factory window with 25 employees. It should include a mix of closed and open sessions, multiple operation types, realistic `standard_seconds_per_unit`, output quantities, defect quantities, and durations derived from target productivity bands.

The employee productivity report only has a valid score when a session is CLOSED, has positive duration, positive reported quantity, and the operation has a positive `standard_seconds_per_unit`. Preview seed data must satisfy those constraints for the majority of historical sessions while retaining a small number of intentional invalid examples for the "Không đủ dữ liệu" state.

Recommended target shape for `REPORT_30_DAYS`:

- 25 employees
- 8-15 production orders
- 20-40 operations
- 200-400 CLOSED sessions across the most recent 30 business days
- 6-12 OPEN sessions
- multiple departments/teams and operation types
- target session-productivity bands roughly distributed across <60%, 60-79%, 80-99%, 100-115%, and >115%
- deterministic seed output: reseeding the same preset should reproduce the same rows and expected metrics

Session duration should be derived from the desired productivity instead of being independent random data:

```
expected_seconds = standard_seconds_per_unit * (good_qty + defect_qty)
actual_seconds = expected_seconds / (target_productivity_percent / 100)
```

This keeps the preview report realistic and prevents impossible productivity values caused by unrelated random duration/output generation.

## Safety

Direct DB mutation is allowed only inside UI Preview Lab and only when all preview ownership guards pass. Never reset, truncate, seed, copy from, or otherwise mutate a current/live MESFlow database.
