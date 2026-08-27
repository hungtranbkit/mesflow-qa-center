from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def test_bug_report_ui_and_routes_present():
    html=(ROOT/'templates'/'demo.html').read_text(encoding="utf-8")
    js=(ROOT/'static'/'demo.js').read_text(encoding="utf-8")
    agent=(ROOT/'agent.py').read_text(encoding="utf-8")
    assert 'Copy for GPT' in html
    assert 'Download Report' in html
    assert 'Evidence ZIP' in html
    assert '/bug-report' in js
    assert '/bug-bundle.zip' in js
    assert 'MESFLOW_QA_GPT_BUG_REPORT_V1' in agent
    assert 'GPT_BUG_REPORT.md' in agent
    assert 'GPT_BUG_BUNDLE.zip' in agent

def test_bug_report_redacts_secrets_and_collects_context():
    agent=(ROOT/'agent.py').read_text(encoding="utf-8")
    assert 'Bearer ***' in agent
    assert '_demo_system_snapshot' in agent
    assert '_demo_target_snapshot' in agent
    assert '_demo_db_snapshot' in agent
    assert 'recent_runner_logs' in agent
    assert 'recent_actions' in agent
    assert 'screenshots' in agent
