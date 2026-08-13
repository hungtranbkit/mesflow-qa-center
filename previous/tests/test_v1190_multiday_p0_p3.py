from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def txt(rel): return (ROOT/rel).read_text(encoding='utf-8')

def test_soak_imports_p0_p3_regression():
    s=txt('scenarios/realtime_factory_soak_test.py')
    for token in ['run_periodic_regression','campaign_summary','--p0-p3-regression','--strict-p0-p1','P0-IDEMPOTENCY-START','P0-IDEMPOTENCY-FINISH','P0-CLOSED-REFINISH-BLOCK','P0-DOUBLE-OPEN-BLOCK','P0-QUALITY-BOUNDS']:
        assert token in s

def test_p0_p3_matrix_contains_all_priorities():
    s=txt('scenarios/p0_p3_regression.py')
    required=['P0-AGGREGATE-RECONCILE','P0-SESSION-SHIFT-BOUNDARY','P1-COMPLETED-CANCELLED-START-BLOCK','P1-DEPENDENCY-INPUT-BLOCK','P1-ACTION-ERROR-LOG','P1-NO-ORPHAN-SESSION','P2-SCHEDULE-PRIORITY','P2-WIP-FIRST-OP','P2-TIMEZONE','P3-DASHBOARD-REPORTS','P3-KIOSK-HEARTBEAT','P3-STATE-RECOVERY','P3-API-HEALTH']
    for token in required: assert token in s

def test_strict_required_coverage_exit_contract():
    s=txt('scenarios/realtime_factory_soak_test.py')
    assert "summary['failed_required']" in s
    assert "summary['missing_required']" in s
    assert 'return 2' in s
