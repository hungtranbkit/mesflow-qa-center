# MESFlow QA Center v1.18.1 - Windows UTF-8 test fix

- All source-inspection tests now call `Path.read_text(encoding="utf-8")`.
- `run_tests.bat` forces `PYTHONUTF8=1` and `PYTHONIOENCODING=utf-8`.
- Added regression tests preventing bare `read_text()` from returning.
- Fixes Windows Python 3.13 cp1252 `UnicodeDecodeError` on Vietnamese UTF-8 source/assets.
