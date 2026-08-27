from pathlib import Path
from version_contract import assert_version_contract
ROOT=Path(__file__).resolve().parents[1]

def test_version_and_retry_contract():
    assert_version_contract(ROOT)
    core=(ROOT/'scenarios/lifecycle_core.py').read_text(encoding="utf-8")
    soak=(ROOT/'scenarios/realtime_factory_soak_test.py').read_text(encoding="utf-8")
    assert '521,522,523,524' in core
    assert '--fallback-base-url' in soak
    assert 'KIOSK-HB-DEFER' in soak

def test_legacy_prefix_cleanup_is_permanently_disabled():
    agent=(ROOT/'agent.py').read_text(encoding="utf-8")
    assert 'LEGACY_QA_CLEANUP_DISABLED' in agent
    assert '@app.post("/api/database/reset")' in agent

def test_ui_uses_disposable_database_reset():
    js=(ROOT/'static/app.js').read_text(encoding="utf-8")
    assert '/api/database/demo/reset' in js
    assert '/api/cleanup' not in js
