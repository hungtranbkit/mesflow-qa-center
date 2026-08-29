# MESFlow QA / Operator Center

Target application source:

`../mesflow`

Trước khi viết hoặc sửa scenario:

1. Đọc implementation MESFlow liên quan.
2. Đọc `../mesflow/PRODUCT.md` nếu liên quan business rule.
3. Kiểm tra UI/API hiện tại.
4. Sau đó mới sửa QA scenario.

Không giả định scenario cũ luôn đúng.

## Primary testing model

Ưu tiên kiểm thử từ góc nhìn người dùng:

Browser
→ UI
→ API
→ Database effects.

Playwright là primary tool cho Operator/Demo scenarios.

API helper chỉ dùng cho:

* setup;
* cleanup;
* assertions;
* health checks;
* diagnostics.

## UI Preview Lab — isolated database exception

UI Preview Lab (`engine/preview_manager.py`, guarded by `engine/preview_guard.py`) là capability riêng, không phải Functional QA target. Để dựng chính xác timestamp/state phục vụ review giao diện, seed script được phép ghi trực tiếp database **chỉ** khi đồng thời thỏa tất cả guard:

- `MESFLOW_UI_PREVIEW=1` trong MESFlow preview container;
- database hiện tại có tên bắt đầu `mesflow_ui_`;
- Docker resource do QA Center tạo có label `com.mesflow.qa.preview=1`;
- database/container/network là resource riêng của preview, không dùng `mesflow-postgres`, không dùng `DATABASE_URL` hiện tại.

Seed/reset/delete phải fail-closed nếu thiếu guard. Không clone dữ liệu thật. Preview bắt đầu từ PostgreSQL trắng, dùng chính MESFlow image chạy migrations hiện tại, sau đó mới chạy deterministic seed.

Khi MESFlow thêm màn hình/business state mới, cập nhật preset tương ứng trong `engine/preview/presets.py`. Functional QA vẫn phải đi qua behavior/API thật; Preview Lab không thay thế validation nghiệp vụ.

## Stable selectors

Ưu tiên:

1. `data-testid`
2. role/aria
3. stable id
4. text semantic

Tránh:

* nth-child;
* selector phụ thuộc sâu vào DOM;
* CSS class chỉ dùng để styling.

Nếu MESFlow thiếu selector ổn định, có thể bổ sung `data-testid` ở sibling MESFlow project.

## Artifacts

Mỗi run có thể tạo:

* report;
* screenshots;
* Playwright trace;
* video;
* narration.

## Test data

Chỉ thao tác data có prefix:

`CODEX-`
`QA-`
`CODEX-DEMO-`

Không cleanup dữ liệu ngoài prefix. UI Preview Lab có thể reset toàn bộ **preview database** sau khi vượt qua guard ở trên; tuyệt đối không áp dụng reset đó cho target database.
