from pathlib import Path
import importlib.util
ROOT=Path(__file__).resolve().parents[1]

def test_demo_runner_and_ui_are_packaged():
    assert (ROOT/'demo_runner.py').exists()
    index=(ROOT/'templates/index.html').read_text(encoding='utf-8')
    html=(ROOT/'templates/demo.html').read_text(encoding='utf-8')
    js=(ROOT/'static/demo.js').read_text(encoding='utf-8')
    assert 'href="/demo"' in index
    assert 'Demo Center' in html and 'runDemo' in html
    assert '/api/demo/' in js and "test_type:'demo'" in js

def test_agent_exposes_demo_contract():
    text=(ROOT/'agent.py').read_text(encoding='utf-8')
    version=(ROOT/'VERSION').read_text(encoding='utf-8').strip()
    assert f'APP_VERSION = "{version}"' in text
    assert 'DEMO_SCENARIOS' in text
    assert 'def demo_worker' in text
    assert '/api/demo/scenarios' in text
    assert '/api/demo/<run_id>/live.png' in text

def test_version_bumped():
    assert (ROOT/'VERSION').read_text(encoding='utf-8').strip()


def test_wrapper_release_version_is_synced():
    wrapper=ROOT.parent
    version=(ROOT/'VERSION').read_text(encoding='utf-8').strip()
    assert (wrapper/'VERSION').read_text(encoding='utf-8').strip()==version
    assert f'mesflow-qa-center:{version}' in (wrapper/'compose.yml').read_text(encoding='utf-8')


def test_presenter_controls_are_packaged():
    html=(ROOT/'templates/demo.html').read_text(encoding='utf-8')
    js=(ROOT/'static/demo.js').read_text(encoding='utf-8')
    agent=(ROOT/'agent.py').read_text(encoding='utf-8')
    runner=(ROOT/'demo_runner.py').read_text(encoding='utf-8')
    for token in ('demoMode','pauseDemo','resumeDemo','demoPrev','demoNextShot','demoReturnLive'):
        assert token in html
    assert '/control' in js and '/screenshots' in js
    assert 'api_demo_control' in agent and 'api_demo_screenshots' in agent
    assert "--mode" in runner and 'wait_after_step' in runner
