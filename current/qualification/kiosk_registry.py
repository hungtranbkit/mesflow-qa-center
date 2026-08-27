"""Kiosk Test Case Registry (spec section 3): a versioned, data-driven
catalogue of every kiosk testcase QA Center can run, PLUS the policy-driven
profiles (section 2) and the independent multi-axis certification rollup
(section 29) that reads real suite/scenario evidence to compute them.

Deliberately NOT a second execution engine. Every testcase here maps to a
`scenario_key` that `qualification/kiosk_emulator.py`'s `KioskEmulatorRunner`
(software) or `qualification/esp_hil.py`'s `EspHilRunner` (physical) already
executes and persists through the shared `ScenarioRunner`/`EvidenceStore`
contract (same `qa_scenario_runs`/`qa_suite_runs`/evidence tables everything
else in this codebase uses). This module is metadata + a read-only rollup
over that data, not a new store.

REGISTRY_VERSION bumps whenever a testcase's identity/expectations change
in a way that would make an old evidence row's claims stale.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .store import connect

REGISTRY_VERSION = "kiosk-v1"


@dataclass(frozen=True)
class KioskTestCase:
    testcase_id: str
    name: str
    category: str
    criticality: str  # CRITICAL | STANDARD
    applicable_driver: str  # KIOSK_EMULATOR | ESP_HIL
    scenario_key: str  # what kiosk_emulator.py / esp_hil.py actually records
    preconditions: str = ""
    expected_kiosk_state: str = ""
    expected_api_result: str = ""
    expected_db_result: str = ""
    timeout_seconds: int = 30
    retry_policy: str = "no-retry"
    cleanup: str = "scenario-owned fixtures (fresh_employee/device per run), no shared mutation"
    feature_mapping: tuple[str, ...] = field(default_factory=tuple)


CATEGORIES = ["BOOT_DEVICE", "EMPLOYEE_SCAN", "OPERATION_SCAN", "KEYPAD_QUANTITY",
              "SESSION_WORKFLOW", "GOOD_DEFECT_REWORK", "INVALID_INPUT", "NETWORK",
              "OFFLINE_RECONNECT", "BACKEND_FAILURE", "DUPLICATE_IDEMPOTENCY",
              "REBOOT_RECOVERY", "UI_DISPLAY", "PERFORMANCE", "STRESS_SOAK", "OTA",
              "PHYSICAL_HIL"]

CATEGORY_LABEL_VI = {
    "BOOT_DEVICE": "Khởi động thiết bị", "EMPLOYEE_SCAN": "Quét nhân viên",
    "OPERATION_SCAN": "Quét công đoạn", "KEYPAD_QUANTITY": "Bàn phím / Sản lượng",
    "SESSION_WORKFLOW": "Luồng Session", "GOOD_DEFECT_REWORK": "Đạt / NG / Sửa lại",
    "INVALID_INPUT": "Đầu vào không hợp lệ", "NETWORK": "Mạng", "OFFLINE_RECONNECT": "Offline / Kết nối lại",
    "BACKEND_FAILURE": "Lỗi backend", "DUPLICATE_IDEMPOTENCY": "Trùng lặp / Idempotency",
    "REBOOT_RECOVERY": "Khởi động lại / Phục hồi", "UI_DISPLAY": "Giao diện",
    "PERFORMANCE": "Hiệu năng", "STRESS_SOAK": "Chịu tải / Soak", "OTA": "Cập nhật firmware (OTA)",
    "PHYSICAL_HIL": "Phần cứng thật (HIL)",
}

# --- Registry: every testcase kiosk_emulator.py / esp_hil.py actually runs.
# `scenario_key` values for the first 13 entries match the exact keys
# already produced by kiosk_emulator.py before this phase (audited, not
# guessed); entries after that map to new scenario functions added in this
# phase (see kiosk_emulator.py's `_new_scenarios` section) or to esp_hil's
# suite-level recording.
REGISTRY: dict[str, KioskTestCase] = {tc.testcase_id: tc for tc in [
    KioskTestCase("KC-001", "Quét nhân viên hợp lệ", "EMPLOYEE_SCAN", "CRITICAL", "KIOSK_EMULATOR",
                  "sessions.lifecycle.kiosk_valid_employee_scan",
                  expected_kiosk_state="WAIT_OPERATION", expected_api_result="accepted:true",
                  feature_mapping=("kiosk.web_legacy_v2",)),
    KioskTestCase("KC-002", "Quét công đoạn tạo Session", "OPERATION_SCAN", "CRITICAL", "KIOSK_EMULATOR",
                  "sessions.lifecycle.kiosk_operation_scan_creates_session",
                  expected_kiosk_state="SESSION_ACTIVE", expected_db_result="work_sessions.status=OPEN"),
    KioskTestCase("KC-003", "Kết thúc Session với sản lượng hợp lệ", "GOOD_DEFECT_REWORK", "CRITICAL",
                  "KIOSK_EMULATOR", "quality.quantities_rework.kiosk_session_close_with_quantities",
                  expected_db_result="work_sessions CLOSED, good/defect/rework khớp kiosk"),
    KioskTestCase("KC-004", "Kết thúc Session với sản lượng bằng 0", "KEYPAD_QUANTITY", "STANDARD",
                  "KIOSK_EMULATOR", "sessions.lifecycle.kiosk_zero_quantity_close",
                  expected_api_result="accepted:true", expected_db_result="good_qty=0"),
    KioskTestCase("KC-005", "Nhân viên không tồn tại bị từ chối", "INVALID_INPUT", "CRITICAL", "KIOSK_EMULATOR",
                  "sessions.lifecycle.kiosk_invalid_employee_rejected",
                  expected_api_result="error.code=EMPLOYEE_NOT_FOUND", expected_kiosk_state="WAIT_EMPLOYEE (không đổi)"),
    KioskTestCase("KC-006", "Công đoạn không tồn tại bị từ chối", "INVALID_INPUT", "CRITICAL", "KIOSK_EMULATOR",
                  "sessions.lifecycle.kiosk_invalid_operation_rejected",
                  expected_api_result="error.code=OPERATION_NOT_FOUND"),
    KioskTestCase("KC-007", "Công đoạn chưa chạy (PLANNED) bị từ chối", "OPERATION_SCAN", "STANDARD",
                  "KIOSK_EMULATOR", "sessions.lifecycle.kiosk_operation_not_workable_rejected",
                  expected_api_result="error.code=OPERATION_NOT_WORKABLE"),
    KioskTestCase("KC-008", "Quét trùng event_id là idempotent", "DUPLICATE_IDEMPOTENCY", "CRITICAL",
                  "KIOSK_EMULATOR", "sessions.lifecycle.kiosk_duplicate_scan_idempotent",
                  expected_api_result="phản hồi 2 lần giống hệt nhau, không xử lý 2 lần"),
    KioskTestCase("KC-009", "Gửi trùng yêu cầu hoàn thành là idempotent", "DUPLICATE_IDEMPOTENCY", "CRITICAL",
                  "KIOSK_EMULATOR", "quality.quantities_rework.kiosk_repeated_completion_idempotent",
                  expected_db_result="đúng 1 quantity_movements row, không double-count"),
    KioskTestCase("KC-010", "Offline queue → kết nối lại → replay", "OFFLINE_RECONNECT", "CRITICAL",
                  "KIOSK_EMULATOR", "kiosk.offline_recovery.queue_reconnect_replay",
                  expected_kiosk_state="hội tụ về WAIT_OPERATION sau replay"),
    KioskTestCase("KC-011", "Backend mất kết nối rồi phục hồi", "BACKEND_FAILURE", "CRITICAL", "KIOSK_EMULATOR",
                  "kiosk.offline_recovery.backend_unavailable_then_restored",
                  expected_api_result="bootstrap thành công sau khi backend phục hồi"),
    KioskTestCase("KC-012", "Luồng kiosk-web (legacy v1) đầy đủ", "SESSION_WORKFLOW", "STANDARD", "KIOSK_EMULATOR",
                  "kiosk.web_legacy_v2.kiosk_web_protocol"),
    KioskTestCase("KC-013", "Hai kiosk cùng lúc trên cùng 1 công đoạn", "SESSION_WORKFLOW", "STANDARD",
                  "KIOSK_EMULATOR", "sessions.group_supervisor.kiosk_multiple_kiosks_same_operation",
                  expected_db_result="operations.done_qty cộng dồn đúng từ cả 2 kiosk"),
    # New this phase --------------------------------------------------
    KioskTestCase("KC-014", "QR rỗng/rác/quá dài bị từ chối, không tạo dữ liệu", "INVALID_INPUT", "CRITICAL",
                  "KIOSK_EMULATOR", "sessions.lifecycle.kiosk_malformed_qr_rejected",
                  expected_api_result="accepted:false cho mọi biến thể, trạng thái không đổi",
                  expected_db_result="không có work_sessions/employees mới"),
    KioskTestCase("KC-015", "QR nhân viên bị quét nhầm ở bước công đoạn", "INVALID_INPUT", "STANDARD",
                  "KIOSK_EMULATOR", "sessions.lifecycle.kiosk_employee_qr_as_operation_rejected"),
    KioskTestCase("KC-016", "Quét công đoạn trước khi có nhân viên", "SESSION_WORKFLOW", "CRITICAL",
                  "KIOSK_EMULATOR", "sessions.lifecycle.kiosk_operation_before_employee_rejected",
                  expected_kiosk_state="vẫn ở WAIT_EMPLOYEE"),
    KioskTestCase("KC-017", "Quét trùng công đoạn liên tiếp không tạo 2 Session", "OPERATION_SCAN", "STANDARD",
                  "KIOSK_EMULATOR", "sessions.lifecycle.kiosk_duplicate_operation_scan_single_session",
                  expected_db_result="chỉ 1 work_sessions OPEN"),
    KioskTestCase("KC-018", "Sản lượng âm bị từ chối", "KEYPAD_QUANTITY", "STANDARD", "KIOSK_EMULATOR",
                  "quality.quantities_rework.kiosk_negative_quantity_rejected",
                  expected_api_result="accepted:false, không cập nhật DB"),
    KioskTestCase("KC-019", "Sản lượng không phải số nguyên bị từ chối", "KEYPAD_QUANTITY", "STANDARD",
                  "KIOSK_EMULATOR", "quality.quantities_rework.kiosk_non_integer_quantity_rejected",
                  expected_api_result="accepted:false, không cập nhật DB"),
    KioskTestCase("KC-020", "Mất phản hồi sau khi backend đã commit — không đếm trùng", "BACKEND_FAILURE",
                  "CRITICAL", "KIOSK_EMULATOR", "kiosk.offline_recovery.response_lost_after_commit_idempotent",
                  expected_db_result="đúng 1 quantity_movements row dù client retry"),
    KioskTestCase("KC-021", "Mất mạng ở từng giai đoạn workflow rồi hội tụ lại", "NETWORK", "CRITICAL",
                  "KIOSK_EMULATOR", "kiosk.offline_recovery.network_drop_per_phase",
                  expected_kiosk_state="hội tụ đúng, không mất/trùng dữ liệu"),
    KioskTestCase("KC-022", "Hàng đợi offline nhiều sự kiện, replay đúng thứ tự", "OFFLINE_RECONNECT", "STANDARD",
                  "KIOSK_EMULATOR", "kiosk.offline_recovery.offline_queue_many_events",
                  expected_db_result="mọi event replay đúng thứ tự, không mất"),
    KioskTestCase("KC-023", "Replay 2 lần cho cùng 1 sự kiện đã queue không tạo trùng", "OFFLINE_RECONNECT",
                  "STANDARD", "KIOSK_EMULATOR", "kiosk.offline_recovery.offline_queue_duplicate_replay_protected"),
    # Physical HIL (orchestrated, not re-implemented -- see esp_hil.py) --
    KioskTestCase("KC-100", "ESP32 HIL: bộ suite vật lý (boot/scan/keypad/network/reboot)",
                  "PHYSICAL_HIL", "CRITICAL", "ESP_HIL", "esp.hil_ota.{suite}_suite",
                  preconditions="thiết bị ESP32 thật cắm sẵn, serial + HTTP debug port reachable",
                  cleanup="không áp dụng (không mutate thiết bị ngoài suite tự thực hiện)"),
]}


# --- Profiles (spec section 2): policy/config-driven testcase selection ---

PROFILES: dict[str, dict[str, Any]] = {
    "QUICK": {
        "description": "Fast developer smoke: boot/employee/operation/quantity + 1 invalid + reconnect",
        "testcase_ids": ["KC-001", "KC-002", "KC-003", "KC-005", "KC-010"],
        "hil": False,
    },
    "STANDARD": {
        "description": "Toàn bộ testcase phần mềm (functional + negative input)",
        "testcase_ids": [tc for tc in REGISTRY if REGISTRY[tc].applicable_driver == "KIOSK_EMULATOR"],
        "hil": False,
    },
    "RELIABILITY": {
        "description": "Mạng / reconnect / backend gián đoạn / trùng lặp / offline",
        "testcase_ids": [tc.testcase_id for tc in REGISTRY.values()
                         if tc.category in ("NETWORK", "OFFLINE_RECONNECT", "BACKEND_FAILURE", "DUPLICATE_IDEMPOTENCY")],
        "hil": False,
    },
    "STRESS": {
        "description": "Vòng lặp scan/input/reconnect tần suất cao (xem kiosk_stress.py + offline_burst.py)",
        "testcase_ids": [],  # driven by kiosk_stress.run(), not the per-testcase registry
        "hil": False,
        "uses_offline_burst": True,
    },
    "FULL": {
        "description": "STANDARD + RELIABILITY + STRESS, cộng HIL thật nếu có phần cứng (không giả PASS)",
        "testcase_ids": [tc for tc in REGISTRY if REGISTRY[tc].applicable_driver == "KIOSK_EMULATOR"],
        "hil": True,
        "uses_offline_burst": True,
    },
    "HIL_ONLY": {
        "description": "Chỉ các case cần phần cứng ESP32 thật",
        "testcase_ids": ["KC-100"],
        "hil": True,
    },
}


def testcase(testcase_id: str) -> KioskTestCase:
    return REGISTRY[testcase_id]


def list_registry() -> list[dict[str, Any]]:
    return [{"testcase_id": tc.testcase_id, "name": tc.name, "category": tc.category,
            "category_label": CATEGORY_LABEL_VI.get(tc.category, tc.category), "criticality": tc.criticality,
            "applicable_driver": tc.applicable_driver, "scenario_key": tc.scenario_key,
            "preconditions": tc.preconditions, "expected_kiosk_state": tc.expected_kiosk_state,
            "expected_api_result": tc.expected_api_result, "expected_db_result": tc.expected_db_result,
            "timeout_seconds": tc.timeout_seconds, "retry_policy": tc.retry_policy,
            "feature_mapping": list(tc.feature_mapping), "registry_version": REGISTRY_VERSION}
           for tc in REGISTRY.values()]


def list_profiles() -> dict[str, Any]:
    return {name: {**spec, "testcase_count": len(spec["testcase_ids"])} for name, spec in PROFILES.items()}


# --- Certification rollup (spec section 29): independent per-axis status,
# computed from REAL suite/scenario rows for every run of this exact
# artifact SHA (reuses coverage.py's run_ids_for_artifact -- no second
# aggregator). Never blends axes: HIL BLOCKED must never make
# KIOSK_HIL_CERTIFIED read PASS just because software axes did.
CATEGORY_AXIS = {
    "NETWORK": "KIOSK_NETWORK_CERTIFIED",
    "OFFLINE_RECONNECT": "KIOSK_OFFLINE_CERTIFIED",
    "PHYSICAL_HIL": "KIOSK_HIL_CERTIFIED",
    "OTA": "KIOSK_OTA_CERTIFIED",
}
SOFTWARE_CATEGORIES = {"BOOT_DEVICE", "EMPLOYEE_SCAN", "OPERATION_SCAN", "KEYPAD_QUANTITY",
                       "SESSION_WORKFLOW", "GOOD_DEFECT_REWORK", "INVALID_INPUT",
                       "BACKEND_FAILURE", "DUPLICATE_IDEMPOTENCY", "UI_DISPLAY"}


def kiosk_certification(artifact_sha256: str) -> dict[str, Any]:
    from .coverage import run_ids_for_artifact  # local import: avoids a module import cycle at load time

    conn = connect()
    artifact = conn.execute("SELECT id FROM qa_artifacts WHERE sha256=?", (artifact_sha256,)).fetchone()
    result = {"artifact_sha256": artifact_sha256, "run_ids": [],
             "certifications": {axis: "BLOCKED" for axis in
                                ["KIOSK_SOFTWARE_CERTIFIED", "KIOSK_NETWORK_CERTIFIED", "KIOSK_OFFLINE_CERTIFIED",
                                 "KIOSK_HIL_CERTIFIED", "KIOSK_OTA_CERTIFIED"]},
             "reasons": {}}
    if not artifact:
        result["reasons"]["*"] = "no qualification run recorded for this artifact SHA256"
        return result
    run_ids = run_ids_for_artifact(artifact["id"])
    result["run_ids"] = run_ids
    if not run_ids:
        result["reasons"]["*"] = "artifact registered but never qualified"
        return result
    placeholders = ",".join("?" for _ in run_ids)
    rows = conn.execute(
        f"""SELECT sr.scenario_key,sr.status FROM qa_scenario_runs sr
            JOIN qa_suite_runs s ON s.id=sr.suite_run_id
            WHERE s.qualification_run_id IN ({placeholders}) AND s.suite_key IN ('kiosk_emulator','esp_hil')""",
        tuple(run_ids)).fetchall()
    by_key: dict[str, str] = {}
    for row in rows:
        # a LATER run of the same artifact always wins for a given
        # scenario_key -- ORDER BY on run_ids already puts them chronological
        by_key[row["scenario_key"]] = row["status"]

    def axis_status(category_set: set[str]) -> tuple[str, str]:
        keys = [tc.scenario_key for tc in REGISTRY.values() if tc.category in category_set
               and "{suite}" not in tc.scenario_key]
        if not keys:
            return "BLOCKED", "no testcase mapped to this axis"
        seen = {k: by_key.get(k) for k in keys}
        missing = [k for k, v in seen.items() if v is None]
        failed = [k for k, v in seen.items() if v == "FAILED"]
        if failed:
            return "FAILED", f"{len(failed)} testcase(s) failed: {', '.join(failed[:3])}"
        if missing:
            return "BLOCKED", f"{len(missing)} testcase(s) never executed for this artifact: {', '.join(missing[:3])}"
        return "PASSED", f"all {len(keys)} testcase(s) passed"

    sw_status, sw_reason = axis_status(SOFTWARE_CATEGORIES)
    result["certifications"]["KIOSK_SOFTWARE_CERTIFIED"] = sw_status
    result["reasons"]["KIOSK_SOFTWARE_CERTIFIED"] = sw_reason

    net_status, net_reason = axis_status({"NETWORK"})
    result["certifications"]["KIOSK_NETWORK_CERTIFIED"] = net_status
    result["reasons"]["KIOSK_NETWORK_CERTIFIED"] = net_reason

    off_status, off_reason = axis_status({"OFFLINE_RECONNECT"})
    result["certifications"]["KIOSK_OFFLINE_CERTIFIED"] = off_status
    result["reasons"]["KIOSK_OFFLINE_CERTIFIED"] = off_reason

    hil_rows = [v for k, v in by_key.items() if k.startswith("esp.hil_ota.")]
    if not hil_rows:
        result["certifications"]["KIOSK_HIL_CERTIFIED"] = "BLOCKED"
        result["reasons"]["KIOSK_HIL_CERTIFIED"] = "no ESP HIL suite executed for this artifact (device not configured/reachable)"
    elif any(v == "FAILED" for v in hil_rows):
        result["certifications"]["KIOSK_HIL_CERTIFIED"] = "FAILED"
        result["reasons"]["KIOSK_HIL_CERTIFIED"] = "ESP HIL suite ran and failed"
    elif all(v == "PASSED" for v in hil_rows):
        result["certifications"]["KIOSK_HIL_CERTIFIED"] = "PASSED"
        result["reasons"]["KIOSK_HIL_CERTIFIED"] = f"{len(hil_rows)} real HIL suite run(s) passed"
    else:
        result["certifications"]["KIOSK_HIL_CERTIFIED"] = "BLOCKED"
        result["reasons"]["KIOSK_HIL_CERTIFIED"] = "ESP HIL suite recorded a non-terminal outcome"

    # OTA: the kiosk firmware repo's own docs/TEST_PLAN.md marks OTA
    # (KIOSK-030) as Phase 6/DESIGNED -- not implemented in firmware yet,
    # so there is genuinely nothing to certify. Never silently PASS.
    result["certifications"]["KIOSK_OTA_CERTIFIED"] = "BLOCKED"
    result["reasons"]["KIOSK_OTA_CERTIFIED"] = "OTA not yet implemented in kiosk firmware (TEST_PLAN.md KIOSK-030: Phase 6/DESIGNED)"

    return result
