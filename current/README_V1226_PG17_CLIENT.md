# QA Center v1.22.7 — PostgreSQL 17 backup client

- Pins `pg_dump`/PostgreSQL client to major version 17 via the official PGDG Debian repository.
- Fixes Database Cleanup backup failure when MESFlow PostgreSQL server is 17.x but the QA image contains Debian bookworm's PostgreSQL 15 client.
- Cleanup safety is unchanged: `pg_dump` must succeed before destructive cleanup proceeds.
