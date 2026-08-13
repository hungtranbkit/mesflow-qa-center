# Windows Hotfix 9
- Bỏ Scheduled Task khỏi đường chạy chính.
- QA được chạy trực tiếp bằng PowerShell Start-Process từ .venv Python.
- Ghi PID, stdout và stderr riêng để chẩn đoán.
- Autostart bằng ProgramData Startup.
- Installer chỉ báo thành công khi process không thoát ngay.
