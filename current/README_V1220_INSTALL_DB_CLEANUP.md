# QA Center 1.22.0 — One-click Docker Install + Full DB Cleanup

- Root `install.sh`: `sudo ./install.sh` builds and deploys Docker image `mesflow-qa-center:1.22.0`.
- Keeps `runtime/` and existing `.env` across upgrades.
- Auto-detects `/opt/mesflow/.env` DATABASE_URL when possible.
- Adds PostgreSQL full cleanup preview/execute with `pg_dump` backup first.
- Full cleanup keeps templates, template children, equipment, users/RBAC, schema metadata, app settings, server generation and working-calendar configuration.
- Cleanup is blocked for non-allowlisted database hosts and while QA runs are active.
