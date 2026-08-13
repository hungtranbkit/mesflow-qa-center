from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def test_windows_installer_contract():
    bat=(ROOT/'install.bat').read_text(encoding='utf-8')
    ps=(ROOT/'start_qa.ps1').read_text(encoding='utf-8')
    helper=(ROOT/'install_helpers.ps1').read_text(encoding='utf-8')
    assert 'v1.18.0' in bat
    assert 'C:\\MESFlowQACenter' in bat and '8095' in bat
    assert '$PSScriptRoot' in helper
    assert 'Start-Process' in ps and 'qa-center-error.log' in ps and '/api/version' in ps

def test_no_old_scheduled_task_dependency_in_primary_installer():
    bat=(ROOT/'install.bat').read_text(encoding='utf-8').lower()
    assert 'schtasks /create' not in bat

def test_ui_controls_cover_all_test_modes():
    h=(ROOT/'templates/index.html').read_text(encoding='utf-8')
    j=(ROOT/'static/app.js').read_text(encoding='utf-8')
    for token in ['functional','api_soak','browser_visual','factory_simulation','realtime_soak']:
        assert token in h+j
    for token in ['realtimeWorkers','realtimeRunDays','realtimeTargetMin','realtimeTargetMax','realtimeForgotRate']:
        assert token in h

def test_dashboard_has_cleanup_and_connection_controls():
    h=(ROOT/'templates/index.html').read_text(encoding='utf-8')
    j=(ROOT/'static/app.js').read_text(encoding='utf-8')
    for token in ['/api/cleanup','/api/check-connection','/api/config','/api/status']:
        assert token in j
