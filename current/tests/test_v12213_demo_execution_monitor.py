from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def test_demo_monitor_ui_contract():
    html=(ROOT/'templates/demo.html').read_text(encoding='utf-8')
    js=(ROOT/'static/demo.js').read_text(encoding='utf-8')
    for token in ('Test Case Monitor','demoCaseInput','demoCaseExpected','demoActionKind','demoHeartbeat','demoActivityLog'):
        assert token in html
    for token in ('renderAction','renderActivity','heartbeat(st)','Có thể bị treo','demoActionKind','demoActivityLog'):
        assert token in js

def test_demo_runner_observability_contract():
    text=(ROOT/'demo_runner.py').read_text(encoding='utf-8')
    for token in ('CASE_DETAILS','build_plan','action_start','action_done','current_action','action_log','heartbeat_at'):
        assert token in text
    for kind in ("'NAVIGATE'","'FOCUS'","'TYPE'","'SELECT'","'CLICK'","'WAIT'"):
        assert kind in text
