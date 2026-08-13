# Windows installer hotfix 1.17.1-2

- Removes Robocopy from `install.bat`.
- Uses PowerShell `Copy-Item` with environment-variable paths, so installation works across drives such as `D:` to `C:` and paths containing spaces.
- Preserves existing `config.json`, `reports`, and `backups`.
- Verifies `agent.py`, `requirements.txt`, and `VERSION` after copying before continuing.
