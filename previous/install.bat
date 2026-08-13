@echo off
setlocal EnableExtensions
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

net session >nul 2>&1
if not "%errorlevel%"=="0" (
  echo [MESFlow QA] Dang yeu cau quyen Administrator...
  powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)

set "SRC=%~dp0"
set "APPDIR=C:\MESFlowQACenter"
set "PORT=8095"
set "PLAYWRIGHT_BROWSERS_PATH=%APPDIR%\ms-playwright"

echo.
echo ============================================================
echo   MESFlow QA Center v1.18.0 - Windows Full Coverage
echo ============================================================
echo.

set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY where python >nul 2>&1 && set "PY=python"
if not defined PY (
  echo [1/8] Khong tim thay Python. Dang thu cai Python 3.12 bang winget...
  where winget >nul 2>&1 || goto :no_python
  winget install --id Python.Python.3.12 -e --silent --accept-package-agreements --accept-source-agreements
  if errorlevel 1 goto :no_python
  set "PATH=%LocalAppData%\Programs\Python\Python312;%LocalAppData%\Programs\Python\Python312\Scripts;%PATH%"
  where py >nul 2>&1 && set "PY=py -3"
  if not defined PY where python >nul 2>&1 && set "PY=python"
)
if not defined PY goto :no_python
echo [1/8] Python OK: %PY%

echo [2/8] Copy source vao %APPDIR% ...
if not exist "%APPDIR%" mkdir "%APPDIR%"
powershell -NoProfile -ExecutionPolicy Bypass -File "%SRC%install_helpers.ps1" -Action copy-source -Destination "%APPDIR%"
if errorlevel 1 goto :copy_fail
if not exist "%APPDIR%\config.json" copy /Y "%SRC%config.json" "%APPDIR%\config.json" >nul
if not exist "%APPDIR%\reports" mkdir "%APPDIR%\reports"
if not exist "%APPDIR%\backups" mkdir "%APPDIR%\backups"
if not exist "%APPDIR%\agent.py" goto :copy_fail
if not exist "%APPDIR%\start_qa.ps1" goto :copy_fail
echo       Copy source OK.

cd /d "%APPDIR%"
if not exist "%APPDIR%\.venv\Scripts\python.exe" (
  echo [3/8] Tao Python virtual environment...
  %PY% -m venv "%APPDIR%\.venv"
  if errorlevel 1 goto :venv_fail
) else (
  echo [3/8] Virtual environment da ton tai.
)
set "VPY=%APPDIR%\.venv\Scripts\python.exe"

echo [4/8] Cai/cap nhat dependencies...
"%VPY%" -m pip install --disable-pip-version-check --upgrade pip
if errorlevel 1 goto :pip_fail
"%VPY%" -m pip install --disable-pip-version-check -r "%APPDIR%\requirements.txt"
if errorlevel 1 goto :pip_fail

echo [5/8] Cai Playwright Chromium...
set "PLAYWRIGHT_BROWSERS_PATH=%APPDIR%\ms-playwright"
"%VPY%" -m playwright install chromium
if errorlevel 1 echo [WARN] Chromium chua cai duoc. QA API van co the chay.

netsh advfirewall firewall delete rule name="MESFlow QA Center 8095" >nul 2>&1
netsh advfirewall firewall add rule name="MESFlow QA Center 8095" dir=in action=allow protocol=TCP localport=%PORT% >nul 2>&1
echo [6/8] Firewall port %PORT% OK.

echo [7/8] Cai autostart vao Windows Startup...
set "STARTUP=%ProgramData%\Microsoft\Windows\Start Menu\Programs\StartUp"
> "%STARTUP%\MESFlow-QA-Center.cmd" echo @echo off
>> "%STARTUP%\MESFlow-QA-Center.cmd" echo powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "C:\MESFlowQACenter\start_qa.ps1"
echo       Autostart OK.

echo [8/8] Khoi dong QA Center...
powershell -NoProfile -ExecutionPolicy Bypass -File "%APPDIR%\start_qa.ps1"
if errorlevel 1 goto :start_fail

powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $r=Invoke-RestMethod -TimeoutSec 5 'http://127.0.0.1:8095/api/version'; Write-Host ('       ONLINE - version ' + $r.version); exit 0 } catch { Write-Host ('       API CHECK FAIL: ' + $_.Exception.Message); exit 1 }"
if errorlevel 1 goto :start_fail

echo.
echo ============================================================
echo   CAI DAT HOAN TAT
echo   QA Center: http://127.0.0.1:%PORT%
echo   Quan ly:   C:\MESFlowQACenter\manage.bat
echo   Log:        C:\MESFlowQACenter\qa-center.log
echo   Error log:  C:\MESFlowQACenter\qa-center-error.log
echo ============================================================
echo.
start "" "http://127.0.0.1:%PORT%"
exit /b 0

:no_python
echo [ERROR] Khong tim thay/cai duoc Python.
goto :error_end
:copy_fail
echo [ERROR] Copy source that bai.
goto :error_end
:venv_fail
echo [ERROR] Khong tao duoc .venv.
goto :error_end
:pip_fail
echo [ERROR] Cai Python dependencies that bai.
goto :error_end
:start_fail
echo [ERROR] QA Center khong khoi dong duoc tren port 8095.
echo.
if exist "%APPDIR%\qa-center-error.log" powershell -NoProfile -Command "Get-Content -LiteralPath '%APPDIR%\qa-center-error.log' -Tail 80"
if exist "%APPDIR%\qa-center.log" powershell -NoProfile -Command "Get-Content -LiteralPath '%APPDIR%\qa-center.log' -Tail 80"
goto :error_end
:error_end
echo.
echo Thu chay truc tiep de xem loi:
echo   powershell -NoProfile -ExecutionPolicy Bypass -File C:\MESFlowQACenter\start_qa.ps1 -Foreground
echo.
pause
exit /b 1
