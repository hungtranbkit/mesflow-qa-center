@echo off
setlocal EnableExtensions
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "APPDIR=%~dp0"
set "PYTEST_TMP=%APPDIR%.pytest_tmp"
set "TMP=%PYTEST_TMP%"
set "TEMP=%PYTEST_TMP%"

if exist "C:\MESFlowQACenter\.venv\Scripts\python.exe" (
  set "PY=C:\MESFlowQACenter\.venv\Scripts\python.exe"
) else if exist "%APPDIR%.venv\Scripts\python.exe" (
  set "PY=%APPDIR%.venv\Scripts\python.exe"
) else (
  set "PY=python"
)

cd /d "%APPDIR%"

echo ============================================================
echo   MESFlow QA Center - Regression Tests v1.19.0
echo ============================================================
echo Python: "%PY%"
echo Pytest temp: "%PYTEST_TMP%"
echo.

rem Never use Windows %%TEMP%% pytest-of-USER folders. They can contain
rem stale junctions/ACLs and make pytest fail before any test starts.
if exist "%PYTEST_TMP%" rmdir /s /q "%PYTEST_TMP%" 2>nul
mkdir "%PYTEST_TMP%" 2>nul
if not exist "%PYTEST_TMP%" (
  echo [ERROR] Khong tao duoc pytest temp: %PYTEST_TMP%
  pause
  exit /b 2
)

"%PY%" -m pytest -q --basetemp="%PYTEST_TMP%\run" tests
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
  echo [PASS] Tat ca testcase da chay xong.
) else (
  echo [FAIL] Pytest exit code=%RC%
)

rem Cleanup only our private test temp; ignore cleanup failures.
rmdir /s /q "%PYTEST_TMP%" 2>nul
pause
exit /b %RC%
