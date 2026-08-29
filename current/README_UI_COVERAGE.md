# Test giao diện · Full UI Coverage

QA Center 1.21.0 bổ sung một chế độ chạy nhanh để dựng có chủ đích các trạng thái MESFlow cần cho review giao diện và đồng thời kiểm tra dữ liệu report.

## Mục tiêu

- Dựng trạng thái đủ đa dạng trong vài phút thay vì chờ mô phỏng nhiều ngày.
- Chỉ đi qua public MESFlow API; không sửa trực tiếp database hoặc timestamp.
- Tạo dữ liệu nhân viên, PO và session có thể nhận biết bằng tiền tố QA để cleanup.
- Kiểm tra cả lỗi backend/report dù UI vẫn render bình thường.
- Giữ nguyên protocol `factory_simulation` để tương thích QA Agent/Deploy Agent cũ; trên UI hiển thị tên mới `Test giao diện · Full UI Coverage`.

## Trạng thái được dựng

### Production Order

- DRAFT: PO đã tạo nhưng chưa Start.
- PARTIAL: PO đang chạy và có session/sản lượng một phần.
- OPEN: PO đang chạy với ít nhất một session OPEN.
- DONE: PO được hoàn tất qua dependency/input-flow thật của Operation.

### Employee / Session

- Nhiều nhân viên active để kiểm tra danh sách và report năng suất.
- Session bình thường có sản lượng OK.
- Session 0 sản lượng để kiểm tra hành vi kết thúc do quét nhầm/chưa làm; nếu backend từ chối thì validation đó được ghi nhận là expected error.
- Session có defect; nếu API không cho defect-only thì QA đóng phiên bằng fallback hợp lệ để không phá dữ liệu.
- Session OPEN để Dashboard/Session Management hiển thị trạng thái đang làm.
- Idempotent replay cùng request id.
- Start mới khi nhân viên đang bận phải bị từ chối.
- QR sai, Operation không tồn tại và Employee không tồn tại phải trả validation error.

## Report oracle

Sau khi dựng state, QA Center gọi và kiểm tra các nguồn sau:

- Dashboard summary
- Dashboard overview
- Control tower
- Production control
- Production Order progress
- Active sessions
- Daily progress
- Daily sessions / timeline
- Recent activity
- Operation sessions report
- Employee performance
- Employee productivity
- Session management
- Session exceptions
- Exception Center
- Production Order detail report
- Operation detail report

Ngoài HTTP success, QA còn đối chiếu chéo:

1. PO vừa tạo phải xuất hiện trong ít nhất một dashboard/report phù hợp.
2. Session OPEN phải xuất hiện trong Active Sessions.
3. Session đã đóng phải xuất hiện trong timeline/session report/session management.
4. Nhân viên QA phải xuất hiện trong Employee Productivity.

Nếu UI render được nhưng dữ liệu report không chứa state vừa tạo, bài test vẫn FAIL.

## Exception phụ thuộc thời gian

Các trạng thái như session chạy quá lâu, quên chốt qua ca/ngày hoặc bất thường dựa trên elapsed-time không được giả bằng cách sửa `started_at` trong DB. Fast UI Coverage ghi `SKIP/TIME-DEPENDENT` cho nhóm này. Dùng `Mô phỏng nhà máy nhiều ngày` để kiểm tra các rule phụ thuộc thời gian thật.

## Cách chạy

Trong QA Center chọn **Test giao diện · Full UI Coverage** và bấm **Chạy Test giao diện**. Giá trị mặc định là 8 nhân viên QA và sản lượng mẫu nhỏ để chạy nhanh.

Kết thúc thành công sẽ có log `UI-COVERAGE-SUMMARY` liệt kê PO/session đã dựng và các màn hình nên review trực tiếp trong MESFlow.
