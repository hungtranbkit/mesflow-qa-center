@echo off
setlocal
chcp 65001 >nul
set "APPDIR=C:\MESFlowQACenter"
set "PORT=8095"
if "%~1"=="" goto menu
if /I "%~1"=="start" goto start
if /I "%~1"=="stop" goto stop
if /I "%~1"=="restart" goto restart
if /I "%~1"=="status" goto status
if /I "%~1"=="logs" goto logs
if /I "%~1"=="open" goto open
goto menu
:menu
echo.
echo MESFlow QA Center Manager
echo 1. Start
echo 2. Stop
echo 3. Restart
echo 4. Status
echo 5. Logs
echo 6. Open browser
echo 0. Exit
set /p C=Chon: 
if "%C%"=="1" goto start
if "%C%"=="2" goto stop
if "%C%"=="3" goto restart
if "%C%"=="4" goto status
if "%C%"=="5" goto logs
if "%C%"=="6" goto open
exit /b 0
:start
powershell -NoProfile -ExecutionPolicy Bypass -File "%APPDIR%\start_qa.ps1"
exit /b %errorlevel%
:stop
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process ^| Where-Object { $_.CommandLine -and $_.CommandLine -like '*C:\MESFlowQACenter*agent.py*' } ^| ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
echo Da stop QA Center.
exit /b 0
:restart
call "%~f0" stop
timeout /t 1 /nobreak >nul
call "%~f0" start
exit /b
:status
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $r=Invoke-RestMethod -TimeoutSec 3 'http://127.0.0.1:%PORT%/api/version'; Write-Host ('ONLINE - version ' + $r.version); exit 0 } catch { Write-Host 'OFFLINE'; exit 1 }"
exit /b %errorlevel%
:logs
if exist "%APPDIR%\qa-center.log" powershell -NoProfile -Command "Get-Content -LiteralPath '%APPDIR%\qa-center.log' -Tail 120"
if exist "%APPDIR%\qa-center-error.log" powershell -NoProfile -Command "Write-Host '--- ERROR LOG ---'; Get-Content -LiteralPath '%APPDIR%\qa-center-error.log' -Tail 120"
exit /b
:open
start "" "http://127.0.0.1:%PORT%"
exit /b
