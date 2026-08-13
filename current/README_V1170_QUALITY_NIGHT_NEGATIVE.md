# MESFlow QA Center v1.17.0

Nâng cấp realtime factory soak test:

- Default 20 nhân viên QA.
- Session tối thiểu 120 phút, tối đa 1440 phút (1 ngày).
- 72% session được chọn trong vùng 80-100% giới hạn trên, nên mặc định đa số kéo dài khoảng 19.2-24 giờ.
- Thời lượng/số lượng session được suy ra từ `standard_seconds_per_unit` đọc từ Operation API; nếu Operation không có thì dùng cycle time đọc từ Template tree API; cuối cùng mới fallback và có WARN.
- Chạy cả ca ngày theo Working Calendar và ca tối mặc định 19:00-07:00.
- Khi finish sinh `good_qty`, `defect_qty` và lỗi sửa được. QA tự dò OpenAPI để tìm tên field repairable/fixable/rework; fallback mặc định `repairable_qty`.
- Khoảng 12% session có lỗi; khoảng 70% nhóm có lỗi có một phần lỗi sửa được.
- Khoảng 10% session đến hạn sẽ gửi một request sai trước finish thật. Các case gồm: số âm, lỗi sửa được lớn hơn tổng lỗi, output vượt kế hoạch, và input-flow không khớp nguồn vào.
- Negative case phải bị HTTP 4xx. Nếu backend nhận dữ liệu sai, QA log `FAIL NEGATIVE-CASE-ACCEPTED`.
- QA thử đọc action error log qua các route phổ biến và tìm marker `QA EXPECT_REJECT` để xác nhận lỗi đã được ghi nhận.

Lưu ý: nếu backend không expose OpenAPI hoặc action-error-log endpoint, QA ghi WARN nhưng vẫn tiếp tục soak test.
