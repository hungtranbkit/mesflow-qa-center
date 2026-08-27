from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def test_main_target_is_built_in_local_and_esc_defined():
    html=(ROOT/'templates/index.html').read_text(encoding="utf-8")
    js=(ROOT/'static/app.js').read_text(encoding="utf-8")
    agent=(ROOT/'agent.py').read_text(encoding="utf-8")
    assert 'id="mainTargetPreset"' in html
    assert 'id="base_url" type="hidden"' in html
    assert 'This machine' in html and 'Built-in' in html
    assert 'Custom URL' not in html
    assert 'const esc=' in js
    assert "base_url:internal" in js
    assert 'base = QA_INTERNAL_URL if QA_INTERNAL_ONLY' not in agent
    assert 'base["base_url"] = QA_INTERNAL_URL' in agent
    assert '"mode": "built-in-local"' in agent
