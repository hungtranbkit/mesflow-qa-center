# MESFlow QA / Operator Center

Target application source: `../mesflow`.

Trước khi sửa scenario: đọc implementation MESFlow liên quan, business rule, UI/API hiện tại; không giả định scenario cũ luôn đúng.

## Primary testing model

Functional QA ưu tiên Browser → UI → API → Database effects. Playwright là primary tool; API helper dùng cho setup, cleanup, assertions, health checks và diagnostics.

## UI Preview Lab — isolated database exception

UI Preview Lab là capability riêng, không phải Functional QA target. Để dựng chính xác timestamp/state phục vụ review giao diện, seed script được phép ghi trực tiếp database **chỉ** khi đồng thời thỏa tất cả guard:

- `MESFLOW_UI_PREVIEW=1` trong MESFlow preview container;
- database hiện tại có tên bắt đầu `mesflow_ui_`;
- Docker resource do QA Center tạo có label `com.mesflow.qa.preview=1`;
- database/container/network là resource riêng của preview, không dùng `mesflow-postgres`, không dùng `DATABASE_URL` hiện tại.

Seed/reset/delete phải fail-closed nếu thiếu guard. Không clone dữ liệu thật. Preview bắt đầu từ PostgreSQL trắng, dùng chính MESFlow image chạy migrations hiện tại, sau đó mới chạy deterministic seed.

Khi MESFlow thêm màn hình/business state mới, cập nhật seed preset tương ứng. Functional QA vẫn phải đi qua behavior/API thật; Preview Lab không thay thế validation nghiệp vụ.

## Stable selectors

Ưu tiên `data-testid`, role/aria, stable id, text semantic. Tránh nth-child, selector phụ thuộc sâu DOM và CSS class chỉ dùng styling.

## Test data

Functional QA chỉ thao tác data có prefix `CODEX-`, `QA-`, `CODEX-DEMO-` và không cleanup ngoài prefix. UI Preview Lab có thể reset toàn bộ **preview database** sau khi vượt qua guard ở trên; tuyệt đối không áp dụng reset đó cho target database.
