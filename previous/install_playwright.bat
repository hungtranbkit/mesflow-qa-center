@echo off
cd /d "%~dp0"
call .venv\Scripts\activate.bat
python -m playwright install chromium
pause
