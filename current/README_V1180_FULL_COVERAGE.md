# QA Center v1.18.0 — Full Coverage

- Bổ sung runtime regression tests cho API/UI QA Center.
- Bổ sung safety tests cho cleanup để không xóa record production thật.
- Bổ sung test Windows install/start contract và chống regression trang trắng.
- Bổ sung test session cuối ca, ca đêm, forgotten finish, quality bounds và negative finish.
- Bổ sung ma trận P0/P1/P2 cho MESFlow: rework, completed/cancelled start block, CLOSED↔OPEN aggregate, reconcile OP/PO, Force Delete, dependency cycle, idempotency, permissions, priority score, WIP first-OP, cross-boundary shift, timezone.
- Thêm `run_tests.bat` để chạy toàn bộ regression bằng `.venv` sau khi cài trên Windows.
