from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def test_demo_defaults_to_auto_and_error_panel_is_packaged():
    html=(ROOT/'templates/demo.html').read_text(encoding='utf-8')
    js=(ROOT/'static/demo.js').read_text(encoding='utf-8')
    assert '<option value="auto" selected>Auto</option>' in html
    for token in ('demoErrorPanel','demoErrorCase','demoErrorAction','demoErrorTarget','demoErrorMessage'):
        assert token in html
    assert 'if(a.error)' in js
    assert 'renderError(st)' in js

def test_failed_case_is_recorded_without_immediate_raise():
    runner=(ROOT/'demo_runner.py').read_text(encoding='utf-8')
    block=runner.split('def step(step_id,title,fn):',1)[1].split('def do_action',1)[0]
    assert "results.append({'id':step_id,'title':title,'status':'FAIL'" in block
    assert 'return False' in block
    assert 'raise\n' not in block
    assert "failed=[r for r in results if r.get('status')=='FAIL']" in runner
    assert "return 2 if any(r.get('status')=='FAIL' for r in results) else 0" in runner

def test_seed_failure_writes_explicit_error_state():
    runner=(ROOT/'demo_runner.py').read_text(encoding='utf-8')
    assert "stage':'setup'" in runner
    assert 'error_detail=err' in runner
    assert "return 3" in runner

def test_demo_event_envelope_does_not_collide_with_action_kind():
    runner=(ROOT/'demo_runner.py').read_text(encoding='utf-8')
    assert 'def emit(event_kind: str, **payload)' in runner
    assert "{'kind': event_kind" in runner
