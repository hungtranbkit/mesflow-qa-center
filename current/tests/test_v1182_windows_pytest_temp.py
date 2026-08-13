from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def test_windows_runner_uses_private_basetemp():
    s = (ROOT / "run_tests.bat").read_text(encoding="utf-8")
    assert ".pytest_tmp" in s
    assert "--basetemp=" in s
    assert 'set "TMP=%PYTEST_TMP%"' in s
    assert 'set "TEMP=%PYTEST_TMP%"' in s

def test_windows_runner_forces_utf8():
    s = (ROOT / "run_tests.bat").read_text(encoding="utf-8")
    assert "PYTHONUTF8=1" in s
    assert "PYTHONIOENCODING=utf-8" in s
