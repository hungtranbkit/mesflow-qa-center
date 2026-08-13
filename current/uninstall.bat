@echo off
setlocal
chcp 65001 >nul
net session >nul 2>&1
if not "%errorlevel%"=="0" (
  powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)
set "APPDIR=C:\MESFlowQACenter"
set "TASKNAME=MESFlow QA Center"
echo Stop va go Scheduled Task...
schtasks /End /TN "%TASKNAME%" >nul 2>&1
schtasks /Delete /TN "%TASKNAME%" /F >nul 2>&1
powershell -NoProfile -ExecutionPolicy Bypass -Command "$p=Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine -like '*C:\MESFlowQACenter*agent.py*' }; foreach($x in $p){ Stop-Process -Id $x.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1
netsh advfirewall firewall delete rule name="MESFlow QA Center 8095" >nul 2>&1
echo.
echo Scheduled Task va firewall da go.
echo Du lieu/config van duoc giu tai %APPDIR%.
echo Neu muon xoa hoan toan, xoa thu muc nay bang tay.
pause
