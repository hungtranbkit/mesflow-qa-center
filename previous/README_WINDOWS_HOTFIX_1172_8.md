# Windows Hotfix 8

Fix installer source path handoff on Windows. `install.bat` no longer passes `%~dp0` as a `-Source` command-line argument. `install_helpers.ps1` uses `$PSScriptRoot` as the canonical source directory, eliminating trailing-backslash/quote parsing failures.
