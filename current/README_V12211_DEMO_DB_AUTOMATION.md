# QA Center 1.22.11 — Demo DB Automation

## Goal
One-click demo database preparation and reset. No manual clone sequence is required.

## Prepare Demo DB
1. Read/clone source database `mesflow`.
2. Create disposable `mesflow_demo_template`.
3. Remove runtime/test data only inside that disposable clone.
4. Remove QA/demo-only employees from the clone.
5. Verify master/config invariants.
6. Create `mesflow_demo` from the verified template.

The source database is never passed to the destructive cleanup helper. The helper hard-refuses any database other than the configured demo template.

## Reset Demo DB
Drop/recreate only `mesflow_demo` from `mesflow_demo_template`, then verify invariants.

## UI
The QA Center main page now contains `Demo Database Automation` with:
- Check Demo DB
- Prepare Demo DB
- Reset Demo DB

The PostgreSQL URL is saved once in QA Center config. The database name in that URL is used for credentials/host only; source/target/template names are controlled by guarded configuration.

## CLI contracts
- `scripts/db-demo-workflow.py preview`
- `scripts/db-demo-prepare.sh`
- `scripts/db-demo-reset.sh`

These scripts and the UI call the same guarded Python implementation.
