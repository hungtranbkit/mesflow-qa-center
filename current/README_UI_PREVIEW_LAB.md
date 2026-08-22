# UI Preview Lab

UI Preview Lab là môi trường MESFlow tạm thời do QA Center quản lý để review giao diện bằng dữ liệu seed có chủ đích. Nó tách hoàn toàn khỏi MESFlow đang chạy và dữ liệu hiện tại.

## Kiến trúc

Mỗi preview gồm:

1. `mesflow-ui-db-<id>` — PostgreSQL 17 tạm thời, database `mesflow_ui_<id>`.
2. `mesflow-ui-preview-<id>` — container chạy MESFlow image được chọn.
3. `mesflow-ui-net-<id>` — network riêng giữa app và database.

MESFlow preview publish backend ra host port riêng, mặc định bắt đầu `127.0.0.1:18080`. QA Center tìm port trống tiếp theo nếu cần.

Không copy/restore production database. PostgreSQL bắt đầu trắng. MESFlow image tự chạy `alembic upgrade head` và bootstrap schema/user; sau readiness PASS, QA Center chạy seed script trong chính container MESFlow preview.

## Lifecycle

- **Start Preview**: tạo DB/network/app riêng, migrate schema, seed preset, chờ READY.
- **Open MESFlow Preview**: mở backend riêng trên port preview.
- **Reset / Re-seed**: tạo lại domain data với time anchor mới.
- **Stop**: dừng app + DB nhưng giữ container.
- **Start lại**: start DB + app đã dừng.
- **Delete Environment**: xóa app, DB và network preview.

Ban đầu chỉ quản lý một preview tại một thời điểm để tránh nhầm resource và giảm tải máy.

## Preset

- `FULL_UI`: cover tổng hợp Dashboard, PO, Operation, employee, kiosk, session, report.
- `NORMAL_FACTORY`: dữ liệu nhà máy bình thường, chart/report dễ đọc.
- `PROBLEM_FACTORY`: PO trễ, session mở lâu, lỗi/defect, warning.
- `REPORT_30_DAYS`: lịch sử 30 ngày cho chart/report.
- `EMPTY_STATE`: schema/user nhưng không có production domain data.
- `EDGE_CASES`: tên dài, số liệu lớn, state biên để kiểm tra layout.

Timestamps sinh tương đối với thời điểm seed (`NOW`) nên mỗi lần re-seed dữ liệu luôn phù hợp thời điểm hiện tại.

## Safety

Preview manager chỉ stop/remove resource có Docker label `com.mesflow.qa.preview=1`. Seed script từ chối chạy nếu thiếu `MESFLOW_UI_PREVIEW=1` hoặc database không bắt đầu bằng `mesflow_ui_`.

QA Center cần mount `/var/run/docker.sock` để quản lý preview containers. Đây là quyền mạnh trên Docker host, vì vậy Preview Manager bắt buộc fail-closed theo label/prefix và không cung cấp endpoint chạy Docker command tùy ý.

## Khi MESFlow thêm tính năng

Cập nhật `preview/seed_full_ui.py` để sinh state mới, chọn MESFlow image mới, rồi Start Preview. Chính image mới chịu trách nhiệm migrate database trắng trước khi seed.

UI Preview Lab phục vụ visual/data-state coverage; Functional QA/API/real workflow vẫn là gate độc lập.
