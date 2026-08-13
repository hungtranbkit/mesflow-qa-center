# Windows installer hotfix 1.17.1-1

Fixes `install.bat` failing at step 2 when the package is extracted directly into `C:\MESFlowQACenter`. The installer now detects source=destination and skips self-copy. It also reports the Robocopy exit code on genuine copy failures.
