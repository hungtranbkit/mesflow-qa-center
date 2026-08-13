@echo off
if exist "C:\MESFlowQACenter\manage.bat" (
  call "C:\MESFlowQACenter\manage.bat" start
  start "" http://127.0.0.1:8095
  exit /b
)
echo QA Center chua duoc cai. Hay chay install.bat truoc.
pause
