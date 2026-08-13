from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def test_version_and_retry_contract():
    assert (ROOT/'VERSION').read_text(encoding="utf-8").strip()=='1.19.0'
    core=(ROOT/'scenarios/lifecycle_core.py').read_text(encoding="utf-8")
    soak=(ROOT/'scenarios/realtime_factory_soak_test.py').read_text(encoding="utf-8")
    assert '521,522,523,524' in core
    assert '--fallback-base-url' in soak
    assert 'KIOSK-HB-DEFER' in soak

def test_cleanup_current_and_legacy_prefixes():
    agent=(ROOT/'agent.py').read_text(encoding="utf-8")
    assert '("QAV65817-","QAV65813-")' in agent
    assert 'employee_no' in agent
    assert 'deleted_stations' in agent
    assert 'request_api("delete", f"/api/production-orders/{po_id}/force"' in agent

def test_ui_shows_station_cleanup():
    js=(ROOT/'static/app.js').read_text(encoding="utf-8")
    assert 'deleted_stations' in js
    assert '${st} trạm' in js
