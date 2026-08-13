# Windows installer hotfix 1.17.1-3

- Replaces fragile inline `schtasks /TR` command with `run_qa.bat` launcher.
- Installs Playwright browsers to shared `C:\MESFlowQACenter\ms-playwright`, usable by SYSTEM task.
- Adds startup logging and detailed diagnostics (Scheduled Task result, port owner, log tail) when port 8095 does not become ready.
