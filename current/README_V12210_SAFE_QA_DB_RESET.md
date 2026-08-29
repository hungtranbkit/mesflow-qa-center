# QA Center v1.22.10 — Safe QA Database Reset

- Removes the destructive full-database `TRUNCATE ... CASCADE` cleanup path.
- Adds dedicated QA database reset via `DROP DATABASE` + `CREATE DATABASE ... TEMPLATE mesflow_qa_template`.
- Hard-blocks protected database names: `mesflow`, `postgres`, `template0`, `template1`.
- Requires an allowlisted disposable target (`mesflow_qa`, `mesflow_test`, `mesflow_demo`).
- Verifies the Golden Template before reset and the cloned QA database after reset.
- Default invariants: employees >= 26, work_shifts >= 2, work_shift_intervals >= 4, templates >= 1, users >= 1, and production_orders/parts/operations/work_sessions = 0.
- If post-reset verification fails, the invalid QA database is dropped (fail-closed).
- Legacy `/api/database/full-cleanup*` endpoints return HTTP 410 and can no longer truncate tables.

This release intentionally does not create the Golden Template from the primary database automatically. Baseline creation is an explicit administration step so QA Center can never mutate the primary MESFlow database while preparing a template.
