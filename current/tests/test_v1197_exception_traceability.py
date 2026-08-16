from pathlib import Path
import ast
import importlib.util
import sys
from version_contract import assert_version_contract


ROOT = Path(__file__).resolve().parents[1]


def test_version_and_agent_are_synchronized():
    assert_version_contract(ROOT)


def test_state_reconcile_reports_untracked_live_qa_sessions():
    source = (ROOT / "scenarios/realtime_factory_soak_test.py").read_text(encoding="utf-8")
    ast.parse(source)
    assert "STATE-UNTRACKED-QA-OPEN" in source
    assert "unexpected_open_sessions" in source
    assert "unexpected_qa_open_sessions" in source
    assert "không tự điền sản lượng" in source


def test_start_persists_run_and_expectation_trace_immediately():
    source = (ROOT / "scenarios/realtime_factory_soak_test.py").read_text(encoding="utf-8")
    assert "QA-RUN-" in source
    assert "EXPECTED-FORGOT" in source
    assert "NORMAL" in source
    start = source[source.index("def start_session"):source.index("def discover_finish_quality_fields")]
    assert "save_state(state)" in start


def test_missing_production_password_fails_instead_of_retrying_forever():
    agent = (ROOT / "agent.py").read_text(encoding="utf-8")
    soak = (ROOT / "scenarios/realtime_factory_soak_test.py").read_text(encoding="utf-8")
    assert "Chưa cấu hình mật khẩu MESFlow" in agent
    assert "QA_AUTH_CONFIGURATION" in soak


def test_qa_reports_expected_detected_unexpected_and_missing_anomalies():
    source = (ROOT / "scenarios/realtime_factory_soak_test.py").read_text(encoding="utf-8")
    for field in ('expected_anomaly_generated','expected_anomaly_detected','unexpected_anomaly','anomaly_not_detected'):
        assert field in source
    assert "inspect_session_exception_outcomes(c,state)" in source


def test_reconcile_identifies_live_qa_session_missing_from_state(monkeypatch, tmp_path):
    scenarios = ROOT / "scenarios"
    sys.path.insert(0, str(scenarios))
    try:
        spec = importlib.util.spec_from_file_location("qa_soak_trace_test", scenarios / "realtime_factory_soak_test.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(scenarios))
    live = [{'id': 41, 'status': 'OPEN', 'employee_id': 7, 'employee_no': 'QAV65817-EMP-007',
             'operation_id': 9, 'operation_code': 'QA-OP', 'started_at': '2026-08-12T08:00:00',
             'start_request_id': 'QA-RUN-RUN123-NORMAL-abcdef'}]
    monkeypatch.setattr(module, 'get_open_sessions', lambda client: live)
    monkeypatch.setattr(module, 'log', lambda *args, **kwargs: None)
    state = {'sessions': {}}
    result = module.reconcile_state(object(), state)
    assert result['anomaly_counters']['unexpected_qa_open_sessions'] == 1
    assert result['unexpected_open_sessions'][0]['session_id'] == 41
