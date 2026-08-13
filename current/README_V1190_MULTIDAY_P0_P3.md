# QA Center v1.19.0 — Multi-day P0/P1/P2/P3 Regression

Soak test nhiều ngày giờ chạy regression probe thật, không chỉ tạo tải.

- P0 mỗi session: Start idempotency, double-open block, quality bounds, Finish idempotency, re-finish CLOSED block.
- P0 định kỳ: aggregate CLOSED sessions ↔ operation, session không vượt shift end trừ `forgot_finish`.
- P1 định kỳ: block COMPLETED/CANCELLED, dependency/input block, action-error log, QA ownership, orphan session.
- P2: schedule/priority, WIP first-OP, timestamp/timezone contract.
- P3: system/execution health, dashboard/report, kiosk heartbeat, state resume/reconcile.
- Cuối campaign có `regression_ledger` + `regression_summary` trong state.
- Mặc định `--strict-p0-p1`: P0/P1 có FAIL hoặc chưa từng cover => exit code 2.
- Capability chưa expose API => `SKIP`, không tính PASS.
