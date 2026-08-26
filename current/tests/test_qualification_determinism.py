from datetime import datetime, timezone
from pathlib import Path

import pytest

from qualification.clock import VirtualClock
from qualification.fixtures import FixtureCatalog, FixtureError
from qualification.invariants import evaluate_domain_snapshot


ROOT = Path(__file__).resolve().parents[1]


def test_versioned_fixture_is_complete_and_stable():
    data = FixtureCatalog(ROOT / "qualification" / "fixture_data").load("mesflow-core-v1")
    assert data["employees"][0]["id"].startswith("QA-")
    assert len(data["sha256"]) == 64
    with pytest.raises(FixtureError):
        FixtureCatalog(ROOT / "qualification" / "fixture_data").load("../production")


def test_virtual_clock_requires_explicit_aware_time_and_advances():
    with pytest.raises(ValueError):
        VirtualClock(datetime(2030, 1, 1))
    clock = VirtualClock(datetime(2030, 1, 1, tzinfo=timezone.utc))
    assert clock.advance(hours=9).hour == 9


def test_domain_invariants_detect_duplicate_active_and_bad_progress():
    failures = evaluate_domain_snapshot({
        "sessions": [
            {"id": "QA-S1", "employee_id": "QA-E1", "status": "ACTIVE", "good_qty": 0},
            {"id": "QA-S2", "employee_id": "QA-E1", "status": "ACTIVE", "good_qty": -1},
        ],
        "operations": [{"id": "QA-O1", "status": "COMPLETED", "target_qty": 10, "produced_qty": 2}],
    })
    assert {item["key"] for item in failures} == {
        "NON_NEGATIVE_QUANTITY", "ONE_ACTIVE_SESSION_PER_EMPLOYEE", "COMPLETED_OPERATION_CONSTRAINT"
    }
