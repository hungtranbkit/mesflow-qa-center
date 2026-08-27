from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
AGENT=(ROOT/'agent.py').read_text(encoding='utf-8')
HTML=(ROOT/'templates'/'index.html').read_text(encoding='utf-8')
JS=(ROOT/'static'/'app.js').read_text(encoding='utf-8')


def test_demo_source_is_read_only_by_construction():
    assert 'REFUSE_TRUNCATE_NON_DISPOSABLE_DATABASE' in AGENT
    assert 'actual != expected' in AGENT
    assert '_demo_template_database()' in AGENT
    assert 'TRUNCATE TABLE {} RESTART IDENTITY CASCADE' in AGENT
    assert 'verify_qa_baseline(source_url, require_runtime_empty=False)' in AGENT
    assert '"informational"' in AGENT


def test_demo_prepare_and_reset_endpoints_exist():
    assert '/api/database/demo/preview' in AGENT
    assert '/api/database/demo/prepare' in AGENT
    assert '/api/database/demo/reset' in AGENT


def test_ui_has_one_click_demo_database_workflow():
    assert 'Demo Database Automation' in HTML
    assert 'Chuẩn bị Demo DB' in HTML
    assert 'Reset Demo DB' in HTML
    assert "'/api/database/demo/prepare'" in JS
    assert "'/api/database/demo/reset'" in JS


def test_cli_contracts_exist():
    assert (ROOT/'scripts'/'db-demo-workflow.py').is_file()
    assert (ROOT/'scripts'/'db-demo-prepare.sh').is_file()
    assert (ROOT/'scripts'/'db-demo-reset.sh').is_file()
