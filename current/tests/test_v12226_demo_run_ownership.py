import importlib.util
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_runner():
    sync_api=types.ModuleType("playwright.sync_api")
    sync_api.sync_playwright=lambda: None
    sync_api.TimeoutError=TimeoutError
    sys.modules.setdefault("playwright",types.ModuleType("playwright"))
    sys.modules["playwright.sync_api"]=sync_api
    spec=importlib.util.spec_from_file_location("qa_demo_runner",ROOT/"demo_runner.py")
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def test_generated_identifiers_are_unique_and_owned_by_run():
    runner=load_runner()
    a=runner.make_data("20260821-070000-aaaaaa")
    b=runner.make_data("20260821-070001-bbbbbb")
    assert a["qa_marker"] == "QA_RUN_ID=20260821-070000-aaaaaa"
    for key in ("employee_code","template_code","po_code","part_code"):
        assert a[key] != b[key]
        assert a[key].startswith("QA-DEMO-")


def test_runner_registers_partial_seed_before_scenario_execution():
    text=(ROOT/"demo_runner.py").read_text(encoding="utf-8")
    assert "ownership.json" in text
    assert "registered('employee',emp)" in text
    assert "registered('template',tpl)" in text
    assert "registered('production_order',po)" in text
    assert "cleanup_status':'RETAINED'" in text
    assert "--run-id" in text
    assert "generated_counts" in text
    for kind in ("parts=","operations=","sessions=","trace_events="):
        assert kind in text
    agent=(ROOT/"agent.py").read_text(encoding="utf-8")
    assert 'finalize_ownership("STOPPED")' in agent
    assert 'finalize_ownership("FAILED")' in agent


def test_cleanup_is_selected_run_only_and_has_confirmation_and_audit():
    agent=(ROOT/"agent.py").read_text(encoding="utf-8")
    assert '@app.post("/api/demo/<run_id>/cleanup")' in agent
    assert 'payload.get("confirm_run_id") != run_id' in agent
    assert 'PO_OWNERSHIP_MISMATCH' in agent
    assert 'TEMPLATE_OWNERSHIP_MISMATCH' in agent
    assert 'EMPLOYEE_OWNERSHIP_MISMATCH' in agent
    assert '"cleanup.json"' in agent
    assert "TRUNCATE" not in agent[agent.index('def api_demo_cleanup'):agent.index('def _redact_bug_text')]


def test_demo_ui_exposes_retained_history_view_and_manual_cleanup():
    html=(ROOT/"templates"/"demo.html").read_text(encoding="utf-8")
    js=(ROOT/"static"/"demo.js").read_text(encoding="utf-8")
    for label in ("View Generated Data","Open MESFlow","Clean Demo Data","Demo Runs"):
        assert label in html
    assert "/api/demo/runs" in js
    assert "confirm_run_id" in js
    assert "RETAINED" in js and "CLEANED" in js
