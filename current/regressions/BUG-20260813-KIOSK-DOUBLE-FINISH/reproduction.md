# Kiosk gửi hai request Finish khi double-click

- Severity: P0
- Seed: `20260813`
- MESFlow: `65.8.44.70`
- Browser test: `tests/kiosk_worker_flow.spec.ts`

## Preconditions

Worker có một Session OPEN. Endpoint Finish được làm chậm 250 ms để tạo cửa sổ race.

## Steps

1. Quét thẻ Worker và mở bước nhập sản lượng.
2. Nhập Đạt 3, Lỗi 1, không có lỗi sửa được.
3. Double-click nút xác nhận cách nhau 25 ms.

## Expected

UI khóa submit trong lúc request đang bay và chỉ phát một HTTP Finish request.

## Actual

Playwright ghi nhận 2 HTTP Finish request. Artifact gồm screenshot, video và trace trong `test-results/` của run thất bại.
