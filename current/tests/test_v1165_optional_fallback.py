from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def test_optional_fallback_logic_present():
    s=(ROOT/'scenarios/lifecycle_core.py').read_text(encoding="utf-8")
    assert "NET-FALLBACK-DISABLED" in s
    assert "if idx==0:" in s
    assert "continue" in s
    assert (ROOT/'VERSION').read_text(encoding="utf-8").strip()=="1.19.0"
