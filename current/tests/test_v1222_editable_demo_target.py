from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def test_demo_ui_has_editable_target_and_presets():
    html=(ROOT/'templates'/'demo.html').read_text(encoding='utf-8')
    js=(ROOT/'static'/'demo.js').read_text(encoding='utf-8')
    assert 'demoTargetUrl' in html
    assert 'demoTargetPreset' in html
    assert 'http://mesflow-app:8080' in html
    assert 'http://host.docker.internal:8080' in html
    assert 'target_url:currentTarget()' in js

def test_backend_demo_target_is_independent_of_internal_only():
    text=(ROOT/'agent.py').read_text(encoding='utf-8')
    assert 'def resolve_demo_target' in text
    assert 'demo_target_url' in text
    assert 'requested_target=resolve_demo_target' in text
    assert 'base=resolve_demo_target' in text

def test_linux_compose_exposes_host_gateway():
    wrapper=ROOT.parent
    compose=(wrapper/'compose.yml').read_text(encoding='utf-8')
    assert 'host.docker.internal:host-gateway' in compose
    assert 'MESFLOW_QA_DEMO_TARGET_URL' in compose

def test_version_1222():
    version=(ROOT/'VERSION').read_text(encoding='utf-8').strip()
    assert (ROOT.parent/'VERSION').read_text(encoding='utf-8').strip()==version
