# MESFlow QA Center v1.17.1

QA Center cho MESFlow với realistic multi-day soak test.

## Cài trên Linux

```bash
unzip -o mesflow_qa_center_v1_17_0_quality_night_negative.zip -d mesflow-qa-v1170
cd mesflow-qa-v1170
chmod +x install.sh
sudo ./install.sh
```

Mặc định QA Center lắng nghe cổng **8095**. Mở:

```text
http://IP_SERVER_QA:8095
```

Service:

```bash
mesflow-qa-center status
mesflow-qa-center logs
mesflow-qa-center version
```

## Soak test v1.17.1

- default 20 nhân viên
- session 120-1440 phút
- đa số session gần 1 ngày
- hỗ trợ ca ngày + ca tối 19:00-07:00
- lấy standard_seconds_per_unit từ Operation/Template
- sinh good, defect, repairable/fixable
- có negative cases để xác nhận backend reject + log lỗi

Xem thêm `README_V1170_QUALITY_NIGHT_NEGATIVE.md`.

## v1.19.0 Full Coverage

Bản này mở rộng regression suite để cover QA Center và các rủi ro MESFlow mới.

### Chạy self-test trên Windows
Sau khi cài bằng `install.bat`, chạy:

```bat
C:\MESFlowQACenter\run_tests.bat
```

Self-test dùng chính `.venv` của QA Center nên kiểm tra đúng Flask/requests/psutil đang chạy thật.

Ma trận nghiệp vụ đầy đủ: `TEST_CASES_V1180_FULL_COVERAGE.md`.

Các nhóm chính: UI trắng/static, config lỗi, start/stop/log, kiosk heartbeat, cleanup an toàn, database legacy, Windows lifecycle, session cuối ca/quên chốt, repairable/rework, negative quantity, input mismatch, idempotency, CLOSED↔OPEN aggregate, reconcile OP/PO, dependency cycle, quyền API, Force Delete, scheduling/priority/WIP, shift qua ngày và timezone.
