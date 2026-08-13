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

Không cleanup dữ liệu ngoài prefix.
