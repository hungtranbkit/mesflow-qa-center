@echo off
setlocal EnableExtensions
set "APPDIR=C:\MESFlowQACenter"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PLAYWRIGHT_BROWSERS_PATH=%APPDIR%\ms-playwright"
set "MESFLOW_QA_HOST=127.0.0.1"
set "MESFLOW_QA_PORT=8095"
cd /d "%APPDIR%"
if not exist "%APPDIR%\qa-center.log" type nul > "%APPDIR%\qa-center.log"
echo.>> "%APPDIR%\qa-center.log"
echo ============================================================>> "%APPDIR%\qa-center.log"
echo [%date% %time%] launcher start, user=%USERNAME%>> "%APPDIR%\qa-center.log"
if not exist "%APPDIR%\.venv\Scripts\python.exe" (
  echo [FATAL] Missing .venv\Scripts\python.exe>> "%APPDIR%\qa-center.log"
  exit /b 10
)
if not exist "%APPDIR%\agent.py" (
  echo [FATAL] Missing agent.py>> "%APPDIR%\qa-center.log"
  exit /b 11
)
"%APPDIR%\.venv\Scripts\python.exe" -u "%APPDIR%\agent.py" >> "%APPDIR%\qa-center.log" 2>&1
set "RC=%ERRORLEVEL%"
echo [%date% %time%] agent.py exited RC=%RC%>> "%APPDIR%\qa-center.log"
exit /b %RC%
