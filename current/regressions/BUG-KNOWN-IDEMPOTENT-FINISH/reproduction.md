# Timeout sau Finish rồi retry

- Severity: P0
- Persona: Worker
- Seed: `20260813`

## Steps

1. Start một Session bằng kiosk.
2. Finish với sản lượng hợp lệ; server nhận request nhưng client timeout.
3. Retry đúng payload và `request_id`.
4. Đối chiếu Session, Operation aggregate và ledger.

Expected: chỉ đóng một lần và chỉ cộng sản lượng một lần.
