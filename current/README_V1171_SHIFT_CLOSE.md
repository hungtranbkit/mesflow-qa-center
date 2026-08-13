# MESFlow QA Center v1.17.1 — Shift-close session model

- Session mới chỉ mở khi còn ít nhất 120 phút trong ca.
- Thời lượng vẫn lấy từ `standard_seconds_per_unit` của Operation/Template nhưng bị chặn bởi cuối ca.
- Ca ngày chốt muộn nhất lúc `work_end` (mặc định 17:00); ca tối chốt muộn nhất lúc `night_shift_end` (mặc định 07:00).
- Mặc định 4% session mô phỏng công nhân quên chốt; các session này được nhập số vào sáng ngày làm việc kế tiếp.
- Log `SES-REAL-START` có `shift_end_at` + `forgot_finish`; log `SIM-END` có `forgotten_open_sessions`.
