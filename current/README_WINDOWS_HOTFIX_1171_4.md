# Windows installer hotfix 4

- Rebuilt `install.bat` cleanly; removed duplicated legacy installer tail that could loop on failure.
- Installer no longer waits for port 8095 at the final step. It starts the Scheduled Task, reports current state, then exits.
- Scheduled Task is registered through PowerShell ScheduledTasks APIs, runs as SYSTEM, and calls `run_qa.bat`.
- Playwright browsers stay in `C:\MESFlowQACenter\ms-playwright`, visible to SYSTEM.
- Added `diagnose_windows.bat` for task/port/process/API/log diagnostics.
