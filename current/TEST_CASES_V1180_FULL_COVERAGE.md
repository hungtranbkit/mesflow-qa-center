# MESFlow QA Center v1.18.0 — Full Coverage Test Matrix

Mục tiêu: không chỉ tạo tải, mà phát hiện sai nghiệp vụ, sai aggregate, session treo, regression API/UI và lỗi cài/chạy QA Center.

## A. QA Center self-test

| Nhóm | Test bắt buộc | Kỳ vọng |
|---|---|---|
| Version/health | `/api/version`, `/api/status` | đúng version, HTTP 200 |
| Dashboard | render `/`, inline CSS/JS, no-cache | không trang trắng |
| Config | save/load, config JSON hỏng/BOM | fallback an toàn, dashboard vẫn chạy |
| Run lifecycle | start 5 loại test, invalid type, stop | dispatch đúng, stop được process |
| Logs | run không tồn tại, giới hạn 500 dòng | 404 đúng, không phình response |
| Kiosk prototype | heartbeat/status | cập nhật last_seen và state |
| Cleanup | active-run block, confirmation, preview, delete order | không xóa data thật; PO→employee→station |
| Database legacy | overview, integrity, backup SQLite | chỉ phục vụ legacy; lỗi path rõ ràng |
| Windows | install/start/autostart/log | không phụ thuộc Scheduled Task SYSTEM; port 8095 lên |
| Browser | Chromium launch + dashboard/login/kiosk | không console/page error nghiêm trọng |

## B. MESFlow production workflow — P0

1. **Rework completion**
   - Finish có good/defect/repairable (rework).
   - `repairable <= defect`.
   - Rework input downstream không vượt lượng rework thực có.
   - Rework completion phải cập nhật đúng operation/session/report.

2. **Không Start OP completed/cancelled**
   - OP `COMPLETED` → Start phải 4xx/409.
   - OP `CANCELLED` → Start phải 4xx/409.
   - Không tạo session OPEN ngầm.
   - Có action/error log cho thao tác bị từ chối.

3. **CLOSED↔OPEN aggregate**
   - Session CLOSED cộng good/defect/rework đúng một lần.
   - Supervisor đổi CLOSED→OPEN phải trừ aggregate cũ.
   - OPEN→CLOSED phải cộng lại đúng số hiện tại.
   - Chỉnh quantity của CLOSED chỉ áp delta, không cộng lặp.

4. **Reconcile OP/PO**
   - Tổng OP phải khớp tổng session CLOSED.
   - PO progress phải khớp aggregate operation.
   - Sau adjust/reopen/close/delete vẫn reconcile được.

5. **Force Delete production**
   - Chỉ QA-owned PO được test xóa.
   - Xóa PO phải xử lý session/execution/reference liên quan theo transaction backend.
   - Không còn orphan session/operation.
   - Cleanup không đụng PO/NV/trạm thật.

## C. MESFlow workflow — P1

6. **Dependency cycle**
   - Template/operation dependency A→B→A phải bị từ chối khi validate/save.
   - Self dependency A→A phải bị từ chối.
   - DAG hợp lệ phải lưu/instantiate được.

7. **Idempotency**
   - Gửi cùng `request_id` Start hai lần: chỉ một session được tạo, lần sau là replay.
   - Gửi cùng `request_id` Finish hai lần: aggregate chỉ cộng một lần.
   - Retry sau timeout/network 52x không được duplicate production quantity.

8. **API permissions**
   - Worker/kiosk không được gọi API admin/force-delete/supervisor.
   - Supervisor được adjust session theo quyền nhưng không có quyền hệ thống ngoài scope.
   - Admin đủ quyền.
   - 401/403 phải có log/audit phù hợp.

9. **Không chỉnh trực tiếp aggregate OP**
   - API master-data không được cho client ghi `done_qty/defect_qty/rework_qty` tùy ý.
   - Aggregate phải sinh từ session/adjustment/reconcile.

## D. Scheduling / P2

10. **Priority Score theo schedule từng OP**
    - Hai PO cùng priority nhưng OP gần deadline hơn phải được ưu tiên đúng.
    - Priority thay đổi phải phản ánh schedule/control tower.

11. **WIP first-OP**
    - PO mới chưa có WIP: first OP được ưu tiên mở luồng.
    - PO đang có WIP downstream: không tạo bottleneck do chỉ ưu tiên PO-level.

12. **Shift cross-boundary**
    - Ca đêm qua 00:00 thuộc đúng shift_date.
    - Session bình thường đóng trước cuối ca.
    - Khi còn < minimum session duration: không mở session mới.
    - Chỉ tỷ lệ nhỏ `forgot_finish` được để sang ngày sau.

13. **Timezone**
    - Chuẩn `Asia/Ho_Chi_Minh` cho dashboard, shift, report, session.
    - UTC timestamp từ DB/API khi hiển thị phải quy đổi đúng.
    - Không lệch +7/-7 giờ ở ca qua ngày.

## E. Quantity / negative cases

14. Negative quantity → reject.
15. Repairable/fixable > defect → reject.
16. Output > planned quantity → reject.
17. Input-flow output > upstream available → reject.
18. Finish session đã CLOSED → reject trừ idempotent replay cùng request_id.
19. Start cùng employee khi đang có OPEN session không hợp lệ → reject/skip có log.
20. Start downstream khi dependency/input chưa đủ → reject.

## F. Multi-day realistic soak

- Default 20 workers, >=2 active QA POs.
- Planned quantity 500–1200.
- Session target 120–1440 phút nhưng **cap bởi cuối ca**.
- Tỷ lệ quên chốt mặc định 4%, tối đa cấu hình 20%.
- Đa số cuối ca phải đóng session và nhập số liệu.
- Session quên chốt nhập vào ngày làm việc kế tiếp.
- Ca ngày + ca tối đều chạy.
- Defect và repairable xuất hiện ngẫu nhiên nhưng hợp lệ.
- 10% finish thử negative case trước finish thật.
- Mỗi report cycle kiểm tra dashboard/control tower/schedule/session/reports/KPI/kiosk.
- Report endpoint lỗi là WARN và simulation tiếp tục; production invariant lỗi là FAIL.

## G. Cleanup/data ownership

- Tất cả record do QA tạo phải có namespace/prefix QA rõ ràng.
- Preview trước khi delete.
- Confirmation bắt buộc: `DELETE QA DATA`.
- Không cleanup khi test đang RUNNING.
- Delete theo API MESFlow, không SQL DELETE trực tiếp PostgreSQL.
- Sau cleanup kiểm tra lại không còn QA-owned PO/session/station/employee orphan.

## H. Kết quả

- **PASS**: assertion nghiệp vụ đúng.
- **FAIL**: server nhận dữ liệu sai, aggregate sai, duplicate do idempotency, orphan, permission bypass, UI/API QA hỏng.
- **WARN**: endpoint report phụ chưa có hoặc transient network đã retry được.
- **SKIP/UNSUPPORTED**: capability chưa expose API; không được tính thành PASS.

## I. v1.19.0 — P0/P1/P2/P3 multi-day executable regression

- P0 chạy trực tiếp trên session QA: Start replay, double-open, Finish replay, CLOSED re-finish, quality bounds.
- P0 định kỳ: aggregate reconcile và shift boundary.
- P1 định kỳ: completed/cancelled start block, dependency/input block, error log, QA ownership, orphan session.
- P2: schedule priority/deadline, WIP first-OP signal, timestamp/timezone parse.
- P3: health, dashboard/report, kiosk heartbeat, restart/state recovery.
- `regression_ledger` lưu số lần PASS/FAIL/WARN/SKIP theo từng case.
- `--strict-p0-p1` mặc định bật: P0/P1 FAIL hoặc chưa từng chạy => campaign exit code 2.
