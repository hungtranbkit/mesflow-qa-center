from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def test_new_mesflow_risk_matrix_is_documented():
    text=(ROOT/'TEST_CASES_V1180_FULL_COVERAGE.md').read_text(encoding='utf-8')
    required=['Rework completion','completed/cancelled','CLOSED↔OPEN','Reconcile OP/PO','Force Delete','Dependency cycle','Idempotency','API permissions','Priority Score','WIP first-OP','cross-boundary','timezone','repairable','cleanup']
    for x in required: assert x.lower() in text.lower()

def test_live_soak_keeps_core_production_invariants():
    s=(ROOT/'scenarios/realtime_factory_soak_test.py').read_text(encoding='utf-8')
    for token in ['standard_seconds_per_unit','repairable_qty','input_flow_enabled','input_source_operation_id','SHIFT-NO-NEW-SESSION','forgot_finish','request_id','/api/action-error-logs','/api/logs/action-errors','/api/audit/action-errors']:
        assert token in s
