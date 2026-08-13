from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def test_all_source_read_text_calls_declare_utf8():
    offenders=[]
    for path in (ROOT / "tests").glob("test_*.py"):
        text=path.read_text(encoding="utf-8")
        for i,line in enumerate(text.splitlines(),1):
            token = ".read_" + "text()"
            if token in line:
                offenders.append(f"{path.name}:{i}")
    assert not offenders, "read_text without UTF-8: " + ", ".join(offenders)

def test_utf8_assets_decode_explicitly():
    for rel in ["static/app.js", "templates/index.html", "scenarios/lifecycle_core.py", "agent.py"]:
        (ROOT/rel).read_text(encoding="utf-8")

def test_windows_runner_forces_utf8_mode():
    s=(ROOT/"run_tests.bat").read_text(encoding="utf-8")
    assert "PYTHONUTF8=1" in s
    assert "PYTHONIOENCODING=utf-8" in s
