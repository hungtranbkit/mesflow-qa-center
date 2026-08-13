# Test Cases MESFlow v65.8.17

| ID | Luồng | Kết quả mong đợi |
|---|---|---|
| AUTH-001 | Auto-login hoặc login admin | HTTP 200, có session đăng nhập |
| TPL-001..003 | Seed và chọn Template | Template active, có Part và Operation |
| PO-001..003 | Tạo PO từ Template và Start | PO `IN_PROGRESS`, Operation được sinh đầy đủ |
| LIFE-001 | Tạo tối thiểu 2 PO | Cùng lúc có ít nhất 2 PO `IN_PROGRESS` |
| NEG-001 | Quét QR sai định dạng | HTTP 400, `SCN-002` |
| NEG-002 | Quét OP không tồn tại | HTTP 404, `OP-001` |
| RUN-* | Quét nhân viên → OP → Start → Finish | Session OPEN rồi CLOSED, sản lượng cộng đúng |
| IDEM-START | Gửi lại cùng request_id Start | Trả cùng session, `idempotent_replay=true` |
| IDEM-FINISH | Gửi lại cùng request_id Finish | Không cộng sản lượng lần hai |
| OP-003 | Đủ planned_quantity | Operation `COMPLETED` |
| PO-004 | Tất cả Operation hoàn tất | PO tự chuyển `COMPLETED` |
| TIME-001..002 | Dashboard daily sessions | Hiện từng session đúng ngày, đúng nhân viên/PO/OP |
| DASH-002 | PO đã hoàn tất | Không xuất hiện trong dashboard active; nếu có báo `dashboard_completed_po_visible` |


## TPL-004 / TPL-005 — Validate Template trước khi tạo PO
- Đọc `/api/templates/{id}/tree`.
- Loại Template có mã Part rỗng hoặc trùng trong cùng cây.
- Ghi cảnh báo với danh sách mã trùng.
- Chỉ chọn Template đáp ứng unique `(production_order_id, code)`.
- Tránh lỗi HTTP 500 `uq_parts_po_code` khi instantiate PO.
