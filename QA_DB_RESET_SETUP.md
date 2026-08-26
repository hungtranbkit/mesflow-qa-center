# QA Database Reset setup — v1.22.11

QA Center no longer deletes tables in the primary MESFlow database.

Required topology:

- Primary DB: `mesflow` — never reset by QA Center.
- Golden Template: `mesflow_qa_template` — immutable clean baseline.
- Disposable QA DB: `mesflow_qa` — resettable clone used by the QA MESFlow target.

Minimum baseline verification defaults:

- employees >= 26
- work_shifts >= 2
- work_shift_intervals >= 4
- templates >= 1
- users >= 1
- production_orders = 0
- parts = 0
- operations = 0
- work_sessions = 0

Runtime configuration example:

```env
MESFLOW_QA_DATABASE_URL=postgresql://mesflow:<password>@postgres:5432/mesflow_qa
MESFLOW_QA_DB_ADMIN_URL=postgresql://mesflow:<password>@postgres:5432/postgres
MESFLOW_QA_TEMPLATE_DATABASE=mesflow_qa_template
MESFLOW_QA_DB_ALLOW_RESET=1
MESFLOW_QA_DB_ALLOWED_NAMES=mesflow_qa,mesflow_test,mesflow_demo
```

QA Center intentionally does not auto-create a Golden Template from `mesflow`. Create/approve the baseline as an explicit admin step only after the source data is known clean. This prevents QA Center from mutating or normalizing the primary database while preparing a template.
