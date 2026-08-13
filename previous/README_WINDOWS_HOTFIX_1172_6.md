# Windows Hotfix 6

Fix installer PowerShell parsing errors caused by CMD caret escaping (`^|`) leaking into embedded PowerShell commands.

Changes:
- Added `install_helpers.ps1` for stop-old, copy-source, and register-task operations.
- `install.bat` no longer embeds PowerShell pipelines containing CMD escape characters.
- Copy preserves existing `config.json`, `.venv`, reports, backups, logs and Playwright browser cache.
- Installer validates required files after copy.
