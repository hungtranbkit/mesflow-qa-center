# V1.16.6 Report Resilience

- Report/KPI HTTP 500 được ghi WARN và lưu số lần lỗi.
- Mô phỏng session, PO và kiosk tiếp tục chạy nhiều ngày.
- `last_report_failures` được lưu trong state để điều tra sau.
- Lỗi nghiệp vụ lõi như login, tạo PO, start/finish session vẫn làm test dừng.
