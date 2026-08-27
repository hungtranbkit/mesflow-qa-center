# QA Center v1.21.0 — Demo Center

Demo Center chạy các kịch bản MESFlow qua Chromium/Playwright từ QA Center và hiển thị Live View bằng screenshot cập nhật liên tục.

## Scenarios

- Full Production Demo
- Planning & Production Order
- Kiosk & Realtime
- Quality / Defect / Rework
- Traceability & Audit
- MESFlow Feature Tour

## Safety

- Browser automation thao tác UI thật.
- API chỉ dùng setup/assertion theo AGENTS.md.
- Dữ liệu demo mới dùng prefix `CODEX-DEMO-`.
- Không tự cleanup dữ liệu ngoài prefix demo.
- Demo Runner dùng internal MESFlow URL khi QA Center chạy internal-only.

## UI

Mở QA Center → **Demo Center** → chọn scenario → chọn tốc độ → **Chạy Demo**.

Live View hiển thị browser đang thao tác; danh sách step cập nhật PASS/FAIL. Có nút **Dừng** để terminate runner.
