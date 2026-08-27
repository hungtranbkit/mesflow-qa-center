from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def test_demo_is_separate_page():
    index=(ROOT/'templates/index.html').read_text(encoding='utf-8')
    demo=(ROOT/'templates/demo.html').read_text(encoding='utf-8')
    agent=(ROOT/'agent.py').read_text(encoding='utf-8')
    assert 'id="demoCenter"' not in index
    assert 'href="/demo"' in index
    assert 'MESFLOW PRESENTATION' in demo
    assert '@app.get("/demo")' in agent

def test_demo_auth_preflight_blocks_bad_credentials_before_run():
    agent=(ROOT/'agent.py').read_text(encoding='utf-8')
    runner=(ROOT/'demo_runner.py').read_text(encoding='utf-8')
    js=(ROOT/'static/demo.js').read_text(encoding='utf-8')
    assert 'demo_auth_preflight' in agent
    assert 'INVALID_CREDENTIALS' in agent
    assert 'if test_type == "demo"' in agent
    assert "emit('auth_error'" in runner
    assert '/api/demo/preflight' in js
    assert "$('runDemo').disabled=!ok" in js

def test_demo_assets_present():
    assert (ROOT/'static/demo.css').is_file()
    assert (ROOT/'static/demo.js').is_file()
