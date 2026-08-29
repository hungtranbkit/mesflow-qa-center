from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT.parent


def test_version_contract():
    version=(ROOT / 'VERSION').read_text(encoding='utf-8').strip()
    assert (WRAPPER / 'VERSION').read_text(encoding='utf-8').strip() == version
    assert f'APP_VERSION = "{version}"' in (ROOT / 'agent.py').read_text(encoding='utf-8')
    assert f'APP_VERSION="{version}"' in (WRAPPER / 'install.sh').read_text(encoding='utf-8')


def test_no_destructive_full_cleanup_truncate():
    text = (ROOT / 'agent.py').read_text(encoding="utf-8")
    assert 'FULL_CLEANUP_DISABLED' in text
    assert 'REFUSE_TRUNCATE_NON_DISPOSABLE_DATABASE' in text
    assert 'FULL_CLEANUP_DISABLED' in text
    assert 'CREATE DATABASE {} TEMPLATE {}' in text
    assert 'REFUSE_UNSAFE_DATABASE' in text


def test_protected_db_names_and_golden_template_contract():
    text = (ROOT / 'agent.py').read_text(encoding="utf-8")
    for name in ('mesflow', 'postgres', 'template0', 'template1'):
        assert name in text
    assert 'mesflow_qa_template' in text
    assert 'work_shift_intervals' in text
    assert 'MESFLOW_QA_DB_MIN_EMPLOYEES' in text


def test_ui_calls_reset_api_not_full_cleanup():
    js = (ROOT / 'static' / 'app.js').read_text(encoding="utf-8")
    html = (ROOT / 'templates' / 'index.html').read_text(encoding="utf-8")
    assert '/api/database/demo/preview' in js
    assert '/api/database/demo/prepare' in js
    assert '/api/database/demo/reset' in js
    assert 'Demo Database Automation' in html
    assert 'Database <code>mesflow</code> không bị DELETE/TRUNCATE' in html

def test_legacy_prefix_cleanup_disabled():
    text = (ROOT / 'agent.py').read_text(encoding="utf-8")
    assert 'LEGACY_QA_CLEANUP_DISABLED' in text
    html = (ROOT / 'templates' / 'index.html').read_text(encoding="utf-8")
    assert 'Cleanup Legacy' in html
    assert 'DISABLED SAFELY' in html
