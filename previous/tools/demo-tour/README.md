# MESFlow Automated Demo Tour

Tour dùng Chromium/Playwright, viewport và video 1920x1080. Dữ liệu tạo mới luôn có prefix `DEMO-VIDEO-`; runner không xóa entity ngoài phạm vi này và mặc định không cleanup.

```powershell
$env:MESFLOW_BASE_URL='https://mesflow.net'
$env:MESFLOW_USERNAME='admin'
$env:MESFLOW_PASSWORD='<secret>'
npm run demo:video -- --mode guide
```

`--mode qa` chạy nhanh để kiểm tra. `--mode guide` có con trỏ, highlight, chapter title, caption và nhịp thao tác chậm. Output nằm tại `test-results/demo-tour/`.

Script dùng UI cho các thao tác trình diễn. API chỉ được dùng để chuẩn bị dataset cô lập và xác nhận entity, đúng ranh giới setup/assertion của QA Center. Nếu tour lỗi, runner dừng ngay, giữ video, chụp ảnh màn hình lỗi và ghi diagnostics trong `report.md`.
