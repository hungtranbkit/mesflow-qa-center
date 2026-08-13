from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def test_version_and_ui():
    assert (ROOT/'VERSION').read_text(encoding="utf-8").strip()=='1.19.0'
    html=(ROOT/'templates/index.html').read_text(encoding="utf-8")
    assert 'Mô phỏng nhà máy nhiều ngày' in html and 'realtimeRunDays' in html and 'realtimeTargetMin' in html
def test_real_time_scheduler_contract():
    s=(ROOT/'scenarios/realtime_factory_soak_test.py').read_text(encoding="utf-8")
    for token in ['--run-days','--session-target-minutes-min','--session-target-minutes-max','plan_session','standard_seconds_per_unit','is_work_time','ensure_po_pool','finish_due','inspect_reports','/api/kiosk/bind','/api/station/heartbeat','/api/station/events/sync']:
        assert token in s
def test_clean_installer_version():
    s=(ROOT/'install.sh').read_text(encoding="utf-8")
    assert 'APP_VERSION="1.18.0"' in s and 'rsync -a --delete' in s
