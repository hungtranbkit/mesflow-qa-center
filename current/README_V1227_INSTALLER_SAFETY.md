# QA Center v1.22.7

- Fix source installer version consistency.
- Keep PostgreSQL 17 client for pg_dump against PostgreSQL 17 server.
- Remove destructive `rsync --delete` from workspace source installer.
- Refuse any install target other than `<workspace>/qa-center`.
