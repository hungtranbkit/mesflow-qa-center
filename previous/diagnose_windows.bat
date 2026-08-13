@echo off
setlocal
chcp 65001 >nul
set "APPDIR=C:\MESFlowQACenter"
set "TASKNAME=MESFlow QA Center"
echo ===== QA VERSION =====
if exist "%APPDIR%\VERSION" type "%APPDIR%\VERSION"
echo.
echo ===== SCHEDULED TASK =====
powershell -NoProfile -ExecutionPolicy Bypass -Command "$t=Get-ScheduledTask -TaskName '%TASKNAME%' -ErrorAction SilentlyContinue; if($t){$i=Get-ScheduledTaskInfo -TaskName '%TASKNAME%'; $t ^| Select TaskName,State; $i ^| Select LastRunTime,LastTaskResult,NextRunTime} else {'Task not found'}"
echo.
echo ===== PORT 8095 =====
netstat -ano | findstr ":8095"
echo.
echo ===== PROCESS =====
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process ^| Where-Object { $_.CommandLine -and $_.CommandLine -like '*MESFlowQACenter*' } ^| Select ProcessId,Name,CommandLine ^| Format-List"
echo.
echo ===== API =====
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { Invoke-RestMethod -TimeoutSec 3 'http://127.0.0.1:8095/api/version' ^| ConvertTo-Json -Depth 5 } catch { $_.Exception.Message }"
echo.
echo ===== LOG TAIL =====
if exist "%APPDIR%\qa-center.log" powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-Content -LiteralPath '%APPDIR%\qa-center.log' -Tail 100"
pause
