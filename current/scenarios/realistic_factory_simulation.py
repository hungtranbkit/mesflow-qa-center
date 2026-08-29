from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from datetime import datetime
from typing import Any

import requests

PREFIX = "QAV65813-"
UA = "MESFlow-QA-Center/UI-Coverage"


def log(level: str, case: str, message: str, **data: Any) -> None:
    suffix = (" | " + json.dumps(data, ensure_ascii=False, default=str)) if data else ""
    print(f"[{level}] {case}: {message}{suffix}", flush=True)


def body_of(resp: requests.Response) -> dict[str, Any]:
    try:
        value = resp.json()
        return value if isinstance(value, dict) else {"value": value}
    except Exception:
        return {"raw": resp.text[:800]}


def assert_ok(resp: requests.Response, case: str, expected: tuple[int, ...] = (200, 201)) -> dict[str, Any]:
    body = body_of(resp)
    if resp.status_code not in expected or body.get("ok") is False:
        raise AssertionError(f"{case}: HTTP {resp.status_code} {body}")
    log("PASS", case, f"HTTP {resp.status_code}")
    return body


def contains_any(value: Any, needles: set[str]) -> bool:
    if isinstance(value, dict):
        return any(contains_any(v, needles) for v in value.values())
    if isinstance(value, list):
        return any(contains_any(v, needles) for v in value)
    return str(value) in needles


class MESClient:
    def __init__(self, base: str, username: str, password: str, verify_ssl: bool = False):
        self.base = base.rstrip("/")
        self.s = requests.Session()
        self.s.verify = verify_ssl
        self.s.headers.update({"Accept": "application/json", "User-Agent": UA})
        if not verify_ssl:
            try:
                requests.packages.urllib3.disable_warnings()
            except Exception:
                pass
        if not password:
            raise AssertionError("AUTH-000 Password required")
        assert_ok(self.s.post(self.base + "/api/auth/login", json={"username": username, "password": password}, timeout=30), "AUTH-001 Login")
        assert_ok(self.s.get(self.base + "/api/auth/me", timeout=30), "AUTH-002 Session")

    def get(self, path: str, **kwargs: Any) -> requests.Response:
        return self.s.get(self.base + path, timeout=30, **kwargs)

    def post(self, path: str, payload: dict[str, Any] | None = None, **kwargs: Any) -> requests.Response:
        return self.s.post(self.base + path, json=payload or {}, timeout=30, **kwargs)


def employees(c: MESClient, count: int) -> list[dict[str, Any]]:
    items = assert_ok(c.get("/api/employees?limit=1000"), "EMP-001 List").get("items", [])
    active = [x for x in items if x.get("active")]
    names = ["Nguyễn Văn Hùng", "Trần Minh Tuấn", "Lê Quốc Bảo", "Phạm Hoàng Nam", "Võ Thanh Tùng", "Đặng Văn Phúc", "Bùi Đức Anh", "Hoàng Minh Khang"]
    seq = 1
    while len(active) < count:
        code = f"{PREFIX}EMP-{seq:03d}"
        seq += 1
        if any(str(x.get("employee_no")) == code for x in items):
            continue
        created = assert_ok(c.post("/api/employees", {
            "employee_no": code,
            "name": names[(seq - 2) % len(names)],
            "department": "Xưởng Cơ khí Demo",
            "team": "Tổ UI Coverage",
            "position": "Công nhân vận hành",
            "employment_status": "Đang làm",
            "active": True,
            "qr": f"WF|EMP|{code}",
        }), "EMP-002 Create")["item"]
        items.append(created)
        active.append(created)
    selected = active[:count]
    log("PASS", "EMP-003 Coverage set", f"{len(selected)} nhân viên sẵn sàng")
    return selected


def template(c: MESClient) -> dict[str, Any]:
    assert_ok(c.post("/api/templates/demo/seed"), "TPL-001 Seed")
    items = assert_ok(c.get("/api/templates/available-for-po"), "TPL-002 Available").get("items", [])
    usable = [x for x in items if int(x.get("part_count") or 0) > 0 and int(x.get("operation_count") or 0) > 0]
    if not usable:
        raise AssertionError("TPL-003 Không có template có operation")
    usable.sort(key=lambda x: (int(x.get("operation_count") or 0), int(x.get("part_count") or 0)))
    chosen = usable[0]
    log("PASS", "TPL-003 Selected", str(chosen.get("code")), operations=chosen.get("operation_count"))
    return chosen


def create_po(c: MESClient, tpl: dict[str, Any], label: str, qty: int, start: bool = True) -> dict[str, Any]:
    code = f"{PREFIX}UI-{datetime.now().strftime('%y%m%d%H%M%S')}-{label}-{uuid.uuid4().hex[:4].upper()}"
    b = assert_ok(c.post("/api/production-orders", {
        "template_id": tpl["id"], "code": code, "planned_quantity": qty,
        "priority": "NORMAL", "notes": f"QA UI Coverage: {label}",
    }), f"PO-{label}-001 Create")
    po_id = int(b.get("production_order_id") or b.get("id"))
    if start:
        assert_ok(c.post(f"/api/production-orders/{po_id}/start"), f"PO-{label}-002 Start")
    item = assert_ok(c.get(f"/api/production-orders/{po_id}"), f"PO-{label}-003 Read")["item"]
    return {"id": po_id, "code": code, "qty": qty, "item": item}


def operations(c: MESClient, po_id: int) -> list[dict[str, Any]]:
    items = assert_ok(c.get("/api/operations?limit=2000"), "OP-001 List").get("items", [])
    result = [x for x in items if int(x.get("production_order_id") or 0) == po_id]
    result.sort(key=lambda x: (int(x.get("sort_order") or 0), int(x.get("id") or 0)))
    if not result:
        raise AssertionError(f"Không có operation cho PO {po_id}")
    return result


def open_employee_ids(c: MESClient) -> set[int]:
    rows = assert_ok(c.get("/api/work-sessions"), "SES-MAP Open sessions").get("items", [])
    return {int(x["employee_id"]) for x in rows if str(x.get("status") or "").upper() == "OPEN"}


def start_session(c: MESClient, emp: dict[str, Any], op: dict[str, Any], case: str) -> tuple[int, str]:
    assert_ok(c.post("/api/kiosk-web/scan", {"qr": str(emp.get("qr") or f"WF|EMP|{emp['employee_no']}")}), case + " Scan employee")
    assert_ok(c.post("/api/kiosk-web/scan", {"qr": str(op.get("qr") or f"WF|OP|{op['code']}")}), case + " Scan operation")
    rid = "QA-UI-START-" + uuid.uuid4().hex
    b = assert_ok(c.post("/api/kiosk-web/start", {
        "request_id": rid, "employee_id": emp["id"], "operation_id": op["id"], "device_uuid": "QA-UI-COVERAGE-KIOSK",
    }), case + " Start")
    return int(b["session"]["id"]), rid


def finish_session(c: MESClient, sid: int, good: int, defect: int, note: str, case: str) -> dict[str, Any]:
    rid = "QA-UI-FINISH-" + uuid.uuid4().hex
    return assert_ok(c.post(f"/api/kiosk-web/finish/{sid}", {
        "request_id": rid, "good_qty": good, "defect_qty": defect, "note": note,
    }), case + " Finish")


def available_employee(c: MESClient, pool: list[dict[str, Any]], offset: int = 0) -> dict[str, Any]:
    busy = open_employee_ids(c)
    ordered = pool[offset % len(pool):] + pool[:offset % len(pool)]
    for emp in ordered:
        if int(emp["id"]) not in busy:
            return emp
    raise AssertionError("Tất cả nhân viên đều đang có session OPEN; QA không đóng session thật để giải phóng")


def first_runnable_op(c: MESClient, po: dict[str, Any]) -> dict[str, Any]:
    ops = operations(c, po["id"])
    for op in ops:
        if not op.get("predecessor_operation_id") and not (op.get("input_flow_enabled") and op.get("input_source_operation_id")):
            return op
    return ops[0]


def create_session_states(c: MESClient, emps: list[dict[str, Any]], po_partial: dict[str, Any], po_open: dict[str, Any]) -> dict[str, Any]:
    made: dict[str, Any] = {"closed": [], "open": [], "expected_errors": []}
    op = first_runnable_op(c, po_partial)

    emp = available_employee(c, emps, 0)
    sid, _ = start_session(c, emp, op, "UI-SES-NORMAL")
    finish_session(c, sid, 1, 0, "UI coverage: phiên bình thường", "UI-SES-NORMAL")
    made["closed"].append(sid)

    emp = available_employee(c, emps, 1)
    sid0, _ = start_session(c, emp, op, "UI-SES-ZERO")
    r = c.post(f"/api/kiosk-web/finish/{sid0}", {
        "request_id": "QA-UI-FINISH-" + uuid.uuid4().hex, "good_qty": 0, "defect_qty": 0,
        "note": "UI coverage: quét nhầm rồi kết thúc, chưa làm sản lượng",
    })
    if r.status_code in (200, 201) and body_of(r).get("ok") is not False:
        log("PASS", "UI-SES-ZERO Finish", "Session 0 sản lượng được ghi nhận hợp lệ")
        made["closed"].append(sid0)
    else:
        log("PASS", "UI-SES-ZERO Validation", "Backend từ chối session 0 sản lượng như thiết kế", http=r.status_code, body=body_of(r))
        made["expected_errors"].append("zero_qty_rejected")

    emp = available_employee(c, emps, 2)
    sidd, _ = start_session(c, emp, op, "UI-SES-DEFECT")
    rd = c.post(f"/api/kiosk-web/finish/{sidd}", {
        "request_id": "QA-UI-FINISH-" + uuid.uuid4().hex, "good_qty": 0, "defect_qty": 1,
        "note": "UI coverage: phát hiện 1 sản phẩm lỗi",
    })
    if rd.status_code in (200, 201) and body_of(rd).get("ok") is not False:
        log("PASS", "UI-SES-DEFECT Finish", "Đã tạo session có defect")
        made["closed"].append(sidd)
    else:
        log("WARN", "UI-SES-DEFECT", "API không nhận defect-only; đóng session bằng good_qty=1", http=rd.status_code)
        finish_session(c, sidd, 1, 0, "UI coverage fallback", "UI-SES-DEFECT-FALLBACK")
        made["closed"].append(sidd)

    op_open = first_runnable_op(c, po_open)
    emp_open = available_employee(c, emps, 3)
    sid_open, first_request = start_session(c, emp_open, op_open, "UI-SES-OPEN")
    made["open"].append(sid_open)

    replay = assert_ok(c.post("/api/kiosk-web/start", {
        "request_id": first_request, "employee_id": emp_open["id"], "operation_id": op_open["id"], "device_uuid": "QA-UI-COVERAGE-KIOSK",
    }), "UI-SES-IDEMPOTENT Replay")
    if int(replay["session"]["id"]) != sid_open or not replay.get("idempotent_replay"):
        raise AssertionError("Idempotency replay không trả đúng session")

    dup = c.post("/api/kiosk-web/start", {
        "request_id": "QA-UI-DUP-" + uuid.uuid4().hex, "employee_id": emp_open["id"], "operation_id": op_open["id"], "device_uuid": "QA-UI-COVERAGE-KIOSK",
    })
    if dup.status_code < 400:
        raise AssertionError(f"UI-SES-DUP expected rejection, got {dup.status_code}: {body_of(dup)}")
    log("PASS", "UI-SES-DUP Busy employee", "Duplicate/busy start bị từ chối hợp lệ", http=dup.status_code, body=body_of(dup))
    made["expected_errors"].append("busy_start")
    return made


def negative_cases(c: MESClient) -> None:
    tests = [
        ("NEG-QR-INVALID", "INVALID-QR"),
        ("NEG-OP-NOTFOUND", "WF|OP|NOT-FOUND"),
        ("NEG-EMP-NOTFOUND", "WF|EMP|NOT-FOUND"),
    ]
    for case, qr in tests:
        r = c.post("/api/kiosk-web/scan", {"qr": qr})
        if r.status_code < 400:
            raise AssertionError(f"{case}: dữ liệu sai lại được chấp nhận: {body_of(r)}")
        log("PASS", case, "Validation error đúng kỳ vọng", http=r.status_code, body=body_of(r))


def verify_endpoint(c: MESClient, path: str, case: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    return assert_ok(c.get(path, params=params or {}), case)


def verify_reports(c: MESClient, pos: list[dict[str, Any]], emps: list[dict[str, Any]], sessions: dict[str, Any]) -> None:
    day = datetime.now().strftime("%Y-%m-%d")
    report_calls = [
        ("/api/dashboard/summary", "RPT-001 Dashboard summary", None),
        ("/api/dashboard/overview", "RPT-002 Overview", {"limit": 2000}),
        ("/api/dashboard/control-tower", "RPT-003 Control tower", {"limit": 500}),
        ("/api/production-control", "RPT-004 Production control", {"limit": 2000}),
        ("/api/dashboard/production-orders", "RPT-005 PO progress", {"limit": 500}),
        ("/api/dashboard/active-sessions", "RPT-006 Active sessions", {"limit": 500}),
        ("/api/dashboard/daily-progress", "RPT-007 Daily progress", {"date": day, "limit": 1000}),
        ("/api/dashboard/daily-sessions", "RPT-008 Daily sessions", {"date": day, "limit": 3000}),
        ("/api/dashboard/recent-activity", "RPT-009 Recent activity", {"limit": 500}),
        ("/api/reports/operation-sessions", "RPT-010 Operation sessions", {"from": day, "to": day, "limit": 3000}),
        ("/api/reports/employee-performance", "RPT-011 Employee performance", {"from": day, "to": day, "limit": 10000}),
        ("/api/reports/employee-productivity", "RPT-012 Employee productivity", {"from": day, "to": day, "limit": 1000}),
        ("/api/session-management", "RPT-013 Session management", {"from": day, "to": day, "limit": 3000}),
        ("/api/session-exceptions", "RPT-014 Session exceptions", {"view": "all", "limit": 1000}),
        ("/api/exceptions", "RPT-015 Exception Center", {"view": "action", "page_size": 200}),
    ]
    data: dict[str, dict[str, Any]] = {}
    for path, case, params in report_calls:
        data[path] = verify_endpoint(c, path, case, params)

    qa_po_needles = {str(p["id"]) for p in pos} | {p["code"] for p in pos}
    if not any(contains_any(data[p], qa_po_needles) for p in ("/api/dashboard/overview", "/api/dashboard/control-tower", "/api/production-control", "/api/dashboard/production-orders")):
        raise AssertionError("RPT-X01: Không report/dashboard nào nhìn thấy PO vừa tạo")
    log("PASS", "RPT-X01 PO visibility", "PO QA xuất hiện trong ít nhất một dashboard/report")

    open_ids = {str(x) for x in sessions["open"]}
    if open_ids and not contains_any(data["/api/dashboard/active-sessions"], open_ids):
        raise AssertionError("RPT-X02: Session OPEN không xuất hiện ở dashboard active sessions")
    log("PASS", "RPT-X02 Active session visibility", f"{len(open_ids)} session OPEN được nhìn thấy")

    closed_ids = {str(x) for x in sessions["closed"]}
    session_sources = [data["/api/dashboard/daily-sessions"], data["/api/reports/operation-sessions"], data["/api/session-management"]]
    missing = [sid for sid in closed_ids if not any(contains_any(src, {sid}) for src in session_sources)]
    if missing:
        raise AssertionError(f"RPT-X03: Session đóng không xuất hiện trong report/timeline: {missing[:10]}")
    log("PASS", "RPT-X03 Closed session visibility", f"{len(closed_ids)} session đóng được report nhìn thấy")

    emp_needles = {str(e["id"]) for e in emps} | {str(e.get("employee_no")) for e in emps}
    if not contains_any(data["/api/reports/employee-productivity"], emp_needles):
        raise AssertionError("RPT-X04: Employee productivity không chứa nhân viên QA")
    log("PASS", "RPT-X04 Productivity visibility", "Nhân viên QA xuất hiện trong productivity")

    for po in pos:
        verify_endpoint(c, f"/api/reports/production-orders/{po['id']}", f"RPT-PO-{po['id']} Detail")
        op0 = operations(c, po["id"])[0]
        verify_endpoint(c, f"/api/reports/operations/{op0['id']}", f"RPT-OP-{op0['id']} Detail")

    log("SKIP", "RPT-TIME-DEPENDENT", "Session quá lâu/quên chốt cần thời gian thật; UI Coverage nhanh không sửa timestamp/DB để tạo exception giả")


def complete_small_po(c: MESClient, po: dict[str, Any], emps: list[dict[str, Any]]) -> list[int]:
    done_sessions: list[int] = []
    pending = operations(c, po["id"])
    completed: set[int] = set()
    safety = 0
    while pending and safety < 200:
        safety += 1
        progressed = False
        for op in pending[:]:
            pred = op.get("predecessor_operation_id")
            source = op.get("input_source_operation_id") if op.get("input_flow_enabled") else None
            if pred and int(pred) not in completed:
                continue
            if source and int(source) not in completed:
                continue
            refreshed = assert_ok(c.get(f"/api/operations/{op['id']}"), "COMP-OP Read")["item"]
            remaining = max(0, int(po["qty"]) - int(refreshed.get("done_qty") or 0))
            if remaining:
                emp = available_employee(c, emps, len(completed) + 4)
                sid, _ = start_session(c, emp, refreshed, f"COMP-{op['id']}")
                finish_session(c, sid, remaining, 0, "UI coverage: hoàn tất PO mẫu", f"COMP-{op['id']}")
                done_sessions.append(sid)
            final = assert_ok(c.get(f"/api/operations/{op['id']}"), "COMP-OP Verify")["item"]
            if str(final.get("status") or "").upper() != "COMPLETED":
                raise AssertionError(f"Operation {op['id']} chưa COMPLETED")
            completed.add(int(op["id"]))
            pending.remove(op)
            progressed = True
        if not progressed:
            raise AssertionError("Không thể hoàn tất PO mẫu do dependency/input-flow")
    final_po = assert_ok(c.get(f"/api/production-orders/{po['id']}"), "COMP-PO Verify")["item"]
    if str(final_po.get("status") or "").upper() != "COMPLETED":
        raise AssertionError(f"PO mẫu chưa COMPLETED: {final_po.get('status')}")
    return done_sessions


def run_cycle(c: MESClient, args: argparse.Namespace) -> None:
    assert_ok(c.get("/api/system/health"), "SYS-001 Health")
    assert_ok(c.get("/api/execution/health"), "SYS-002 Execution health")
    emps = employees(c, max(6, min(args.workers, 12)))
    tpl = template(c)

    po_draft = create_po(c, tpl, "DRAFT", max(3, args.planned_quantity), start=False)
    po_partial = create_po(c, tpl, "PARTIAL", max(4, args.planned_quantity + 1), start=True)
    po_open = create_po(c, tpl, "OPEN", max(4, args.planned_quantity + 1), start=True)
    po_done = create_po(c, tpl, "DONE", max(2, args.planned_quantity), start=True)
    pos = [po_draft, po_partial, po_open, po_done]

    session_states = create_session_states(c, emps, po_partial, po_open)
    session_states["closed"].extend(complete_small_po(c, po_done, emps))
    negative_cases(c)
    verify_reports(c, pos, emps, session_states)

    log("PASS", "UI-COVERAGE-SUMMARY", "Đã dựng bộ trạng thái giao diện và kiểm tra report", **{
        "production_orders": {"draft": po_draft["code"], "partial": po_partial["code"], "open_session": po_open["code"], "completed": po_done["code"]},
        "closed_sessions": session_states["closed"], "open_sessions": session_states["open"],
        "expected_error_states": session_states["expected_errors"],
        "manual_review": ["Tổng quan sản xuất", "Điều hành PO", "Năng suất nhân viên", "Timeline/Session", "Session bất thường", "Exception Center", "Production Trace/Material Flow nếu có dữ liệu"],
    })


def main() -> int:
    p = argparse.ArgumentParser(description="MESFlow fast UI coverage state generator + report verifier")
    p.add_argument("--base-url", required=True)
    p.add_argument("--fallback-base-url", default="")
    p.add_argument("--username", default="admin")
    p.add_argument("--password", default="")
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--target-active-pos", type=int, default=2)
    p.add_argument("--planned-quantity", type=int, default=3)
    p.add_argument("--workdays", type=int, default=1)
    p.add_argument("--loop", action="store_true")
    p.add_argument("--interval-seconds", type=int, default=3600)
    p.add_argument("--verify-ssl", action="store_true")
    p.add_argument("--fresh", action="store_true")
    p.add_argument("--db-path", default="")
    args = p.parse_args()
    try:
        while True:
            client = MESClient(args.base_url, args.username, args.password, args.verify_ssl)
            run_cycle(client, args)
            if not args.loop:
                return 0
            log("INFO", "LOOP", f"Chu kỳ tiếp theo sau {args.interval_seconds}s")
            time.sleep(max(60, args.interval_seconds))
    except KeyboardInterrupt:
        log("WARN", "STOP", "Đã dừng theo yêu cầu")
        return 130
    except Exception as exc:
        log("FAIL", "UI-COVERAGE", f"{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
