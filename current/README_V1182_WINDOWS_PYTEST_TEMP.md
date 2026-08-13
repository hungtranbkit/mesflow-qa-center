# v1.19.0 - Windows pytest temp fix

- `run_tests.bat` no longer uses `%LOCALAPPDATA%\Temp\pytest-of-USER`.
- Uses private `.pytest_tmp` under the QA Center folder.
- Sets `TMP` and `TEMP` to the private test directory.
- Passes `--basetemp=.pytest_tmp\run` explicitly to pytest.
- Cleans the private temp directory before and after tests.
- Keeps UTF-8 environment enforcement from v1.18.1.
