from __future__ import annotations

from pathlib import Path

import pytest

from engine.concurrency import run_simultaneously
from engine.consistency_oracle import ConsistencyOracle
from engine.fault_injection import FaultPlan
from engine.replay import load_scenario, save_reproduction, shrink_actions
from engine.scenario_generator import MistakeRates, ScenarioGenerator
from engine.state_model import InvalidTransition, POState, PO_TRANSITIONS, Quantity, transition


def test_generator_is_deterministic_and_has_reproduction_identity():
    left = ScenarioGenerator(20260813).generate_many(50)
    right = ScenarioGenerator(20260813).generate_many(50)
    assert [x.to_dict() for x in left] == [x.to_dict() for x in right]
    assert len({x.scenario_id for x in left}) == 50
    assert all(len(x.fingerprint) == 64 for x in left)
    assert [x.to_dict() for x in left] != [x.to_dict() for x in ScenarioGenerator(7).generate_many(50)]


def test_mistake_rates_are_configurable_and_bounded():
    scenario = ScenarioGenerator(1, MistakeRates(double_click_rate=1, wrong_scan_rate=0)).generate()
    assert "DOUBLE_CLICK" in scenario.mistakes and "WRONG_SCAN" not in scenario.mistakes
    with pytest.raises(ValueError):
        MistakeRates(network_drop_rate=1.01)


def test_state_machine_rejects_terminal_and_skipped_transitions():
    assert transition(POState.DRAFT, POState.READY, PO_TRANSITIONS) == POState.READY
    with pytest.raises(InvalidTransition):
        transition(POState.DRAFT, POState.COMPLETED, PO_TRANSITIONS)
    with pytest.raises(InvalidTransition):
        transition(POState.COMPLETED, POState.RUNNING, PO_TRANSITIONS)


def test_quantity_invariants():
    Quantity(10, 3, 2).validate()
    with pytest.raises(ValueError): Quantity(-1, 0, 0).validate()
    with pytest.raises(ValueError): Quantity(1, 2, 3).validate()


def test_consistency_oracle_compares_all_available_surfaces():
    readers = {
        "sessions": lambda _: [
            {"status": "CLOSED", "good_qty": 7, "defect_qty": 2, "rework_qty": 1},
            {"status": "OPEN", "good_qty": 99, "defect_qty": 0, "rework_qty": 0},
        ],
        "operation": lambda _: {"done_qty": 7, "defect_qty": 2, "rework_qty": 1},
        "dashboard": lambda _: {"good_quantity": 7, "defect_quantity": 2, "repairable_quantity": 1},
        "report": lambda _: {"good_quantity": 8, "defect_quantity": 2, "repairable_quantity": 1},
    }
    result = ConsistencyOracle(readers).check_operation({"operation_id": 42})
    assert not result.ok
    assert [(x.source, x.invariant) for x in result.failures] == [("report", "OPERATION_EQUALS_CLOSED_SESSIONS")]


def test_wip_oracle_detects_overconsumption():
    result = ConsistencyOracle.check_wip({"done_qty": 10}, [{"good_qty_consumed": 8}, {"good_qty_consumed": 3}])
    assert not result.ok and result.observations["available"] == -1


def test_fault_plan_models_received_timeout_and_http_status():
    assert FaultPlan(1).for_variant("TIMEOUT").server_received
    assert FaultPlan(1).for_variant("HTTP_503").status == 503


def test_replay_round_trip_and_reducer(tmp_path: Path):
    scenario = ScenarioGenerator(99).generate()
    target = save_reproduction(tmp_path, "BUG-0001", scenario, {"quantity_once": True})
    assert load_scenario(target) == scenario
    actions = [{"name": name} for name in ("OPEN", "FILTER", "START", "FINISH_TIMEOUT", "RETRY_FINISH", "REPORT")]
    reduced = shrink_actions(actions, lambda xs: {x["name"] for x in xs} >= {"START", "FINISH_TIMEOUT", "RETRY_FINISH"})
    assert [x["name"] for x in reduced] == ["START", "FINISH_TIMEOUT", "RETRY_FINISH"]


@pytest.mark.parametrize("workers", [1, 5, 10, 20, 22])
def test_concurrency_runner_releases_all_supported_worker_levels(workers):
    assert sorted(run_simultaneously(workers, lambda i: i)) == list(range(workers))
