"""Kiosk Test Case Registry / profiles / certification rollup -- structural
contracts, no DB/Docker needed (kiosk_certification() itself needs a real
DB connection since it queries qa_scenario_runs; that's covered separately
in test_kiosk_certification_real.py against a real sandbox)."""
from __future__ import annotations

from qualification.kiosk_registry import (
    CATEGORIES, PROFILES, REGISTRY, REGISTRY_VERSION, list_profiles, list_registry,
    testcase as get_testcase,
)


def test_every_registry_entry_has_a_real_category_and_driver():
    for tc in REGISTRY.values():
        assert tc.category in CATEGORIES, f"{tc.testcase_id} has unknown category {tc.category!r}"
        assert tc.applicable_driver in ("KIOSK_EMULATOR", "ESP_HIL")
        assert tc.criticality in ("CRITICAL", "STANDARD")
        assert tc.scenario_key


def test_testcase_ids_are_unique_and_lookup_works():
    ids = [tc.testcase_id for tc in REGISTRY.values()]
    assert len(ids) == len(set(ids))
    for tid in ids:
        assert get_testcase(tid).testcase_id == tid


def test_registry_version_is_set():
    assert REGISTRY_VERSION


def test_every_profile_only_references_real_testcase_ids():
    for name, spec in PROFILES.items():
        for tid in spec["testcase_ids"]:
            assert tid in REGISTRY, f"profile {name} references unknown testcase {tid}"


def test_quick_profile_is_a_real_subset_not_everything():
    quick = set(PROFILES["QUICK"]["testcase_ids"])
    standard = set(PROFILES["STANDARD"]["testcase_ids"])
    assert quick, "QUICK profile must not be empty"
    assert quick.issubset(standard)
    assert len(quick) < len(standard), "QUICK must be a real smoke subset, not equal to STANDARD"


def test_reliability_profile_only_contains_reliability_categories():
    reliability_categories = {"NETWORK", "OFFLINE_RECONNECT", "BACKEND_FAILURE", "DUPLICATE_IDEMPOTENCY"}
    for tid in PROFILES["RELIABILITY"]["testcase_ids"]:
        assert REGISTRY[tid].category in reliability_categories


def test_hil_only_profile_never_includes_software_testcases():
    for tid in PROFILES["HIL_ONLY"]["testcase_ids"]:
        assert REGISTRY[tid].applicable_driver == "ESP_HIL"


def test_full_profile_declares_hil_but_never_fakes_it():
    # FULL attempts real HIL when hardware is present -- the profile
    # metadata says so, but nothing here claims a PASS; that's only ever
    # decided by esp_hil.py's real detection at run time.
    assert PROFILES["FULL"]["hil"] is True
    assert PROFILES["QUICK"]["hil"] is False
    assert PROFILES["STANDARD"]["hil"] is False


def test_list_registry_and_list_profiles_are_json_shaped():
    items = list_registry()
    assert len(items) == len(REGISTRY)
    for item in items:
        assert item["testcase_id"] and item["category"] and item["registry_version"] == REGISTRY_VERSION
    profiles = list_profiles()
    assert set(profiles) == set(PROFILES)
    for name, spec in profiles.items():
        assert spec["testcase_count"] == len(PROFILES[name]["testcase_ids"])


def test_ota_has_no_registered_software_testcase():
    # OTA is genuinely not implemented in the kiosk firmware yet
    # (mesflow-kiosk-runtime-v2/docs/TEST_PLAN.md KIOSK-030: Phase 6/
    # DESIGNED) -- the registry must not pretend otherwise by inventing an
    # OTA testcase with nothing real behind it.
    assert not [tc for tc in REGISTRY.values() if tc.category == "OTA"]


def test_certification_axes_cover_the_spec_independent_set():
    from qualification.kiosk_registry import kiosk_certification
    import inspect
    axes = {"KIOSK_SOFTWARE_CERTIFIED", "KIOSK_NETWORK_CERTIFIED", "KIOSK_OFFLINE_CERTIFIED",
           "KIOSK_HIL_CERTIFIED", "KIOSK_OTA_CERTIFIED"}
    source = inspect.getsource(kiosk_certification)
    for axis in axes:
        assert axis in source


def test_ota_axis_is_always_blocked_never_a_fake_pass():
    # Static contract: the OTA branch inside kiosk_certification() is a
    # hardcoded BLOCKED with an honest reason, never a query result that
    # could accidentally resolve to PASSED.
    import inspect
    from qualification.kiosk_registry import kiosk_certification
    source = inspect.getsource(kiosk_certification)
    ota_block = source.split('result["certifications"]["KIOSK_OTA_CERTIFIED"]', 1)[1]
    assert '"BLOCKED"' in ota_block.splitlines()[0]
