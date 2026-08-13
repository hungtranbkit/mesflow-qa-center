MESFlow QA Center v1.17.1 - Windows One-Click

CAI DAT:
1. Giai nen ZIP.
2. Double-click install.bat.
3. Chap nhan UAC Administrator.
4. Cho installer hoan tat; trinh duyet se mo http://localhost:8095.

Installer se:
- Tu tim Python; neu chua co se thu cai Python 3.12 bang winget.
- Copy QA Center vao C:\MESFlowQACenter.
- Tao .venv va cai requirements.
- Cai Playwright Chromium.
- Mo Windows Firewall TCP 8095.
- Tao Scheduled Task "MESFlow QA Center" chay luc Windows khoi dong.
- Start QA Center ngay sau khi cai.

QUAN LY:
C:\MESFlowQACenter\manage.bat
hoac:
  manage.bat status
  manage.bat start
  manage.bat stop
  manage.bat restart
  manage.bat logs
  manage.bat open

UPDATE:
- Giai nen ban QA moi.
- Chay install.bat cua ban moi.
- config.json, reports va backups hien tai duoc giu lai.

GO CAI DAT:
- Chay uninstall.bat (giu config/reports).
