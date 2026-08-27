from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

from .evidence import EvidenceStore
from .live import normalized_fingerprint
from .store import connect, now


PAGES = [
    ("dashboard.production_overview.browser", "dashboard", "Dashboard theo ngày"),
    ("production_orders.lifecycle.browser", "production-orders", "Production Order"),
    ("templates.parts.operations.browser", "templates", "Quy trình sản xuất mẫu"),
    ("employees.stations_equipment.browser", "employees", "Quản lý nhân viên"),
    ("quality.quantities_rework.browser", "production-trace", "Production Trace"),
    ("reports.kpi_productivity.browser", "employee-productivity", "Năng suất"),
    ("calendar.shifts.browser", "working-calendar", "Ca làm việc"),
    ("trace.audit.browser", "business-audit", "Nhật ký nghiệp vụ"),
    ("imports.exports.qr.browser", "qr-print", "In tem QR"),
]


def run_browser_suite(run_id: str, target_url: str, evidence_root: Path) -> dict[str, Any]:
    conn = connect()
    evidence = EvidenceStore(evidence_root)
    suite_id = f"suite-{uuid.uuid4().hex}"
    conn.execute("""INSERT INTO qa_suite_runs(id,qualification_run_id,suite_key,layer,required,status,started_at,command_json)
      VALUES(?,?, 'ui_critical','browser',1,'RUNNING',?,'[]')""", (suite_id, run_id, now()))
    conn.commit()
    results = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(record_har_path=str(evidence_root / run_id / "browser.har"))
        context.tracing.start(screenshots=True, snapshots=True, sources=True)
        page = context.new_page()
        page_errors: list[str] = []
        console_errors: list[str] = []
        failed_requests: list[dict[str, Any]] = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
        page.on("response", lambda response: failed_requests.append({"url": response.url, "status": response.status})
                if response.status >= 400 and "/api/" in response.url else None)
        login_scenario = f"scenario-{uuid.uuid4().hex}"
        conn.execute("""INSERT INTO qa_scenario_runs(id,suite_run_id,scenario_key,scenario_version,driver,status,started_at)
          VALUES(?,?,?,'mesflow-browser-v1','BROWSER','RUNNING',?)""",
                     (login_scenario, suite_id, "auth.sessions_roles.browser", now())); conn.commit()
        login_status, login_fp, login_actual = "PASSED", "", {}
        try:
            page.goto(target_url + "/login", wait_until="networkidle")
            if not page.locator("#username").is_visible() or not page.locator("#password").is_visible():
                raise AssertionError("login controls are not visible")
            if page_errors or console_errors or failed_requests:
                raise AssertionError(json.dumps({"page_errors": page_errors, "console_errors": console_errors,
                                                 "failed_requests": failed_requests}, ensure_ascii=False))
            login_actual = {"url": page.url, "username_visible": True, "password_visible": True}
        except Exception as exc:
            login_status = "FAILED"
            login_actual = {"error_class": type(exc).__name__, "message": str(exc)}
            login_fp = normalized_fingerprint("auth.sessions_roles.browser", "login_render", page.url, type(exc).__name__, login_actual)
        login_ev = evidence.write_json(run_id, "browser-login.json", {"scenario": "auth.sessions_roles.browser",
          "status": login_status, "expected": {"login_controls_visible": True, "no_errors": True},
          "actual": login_actual, "fingerprint": login_fp}, kind="BROWSER_EVIDENCE",
          suite_run_id=suite_id, scenario_run_id=login_scenario)
        conn.execute("UPDATE qa_scenario_runs SET status=?,finished_at=?,fingerprint=?,actual_json=? WHERE id=?",
                     (login_status, now(), login_fp, json.dumps(login_actual), login_scenario))
        conn.execute("INSERT INTO qa_attempts(scenario_run_id,attempt_no,status,fingerprint,started_at,finished_at) VALUES(?,1,?,?,?,?)",
                     (login_scenario, login_status, login_fp, now(), now())); conn.commit()
        results.append({"key": "auth.sessions_roles.browser", "status": login_status, "fingerprint": login_fp, "evidence": login_ev})
        page_errors.clear(); console_errors.clear(); failed_requests.clear()
        auth = context.request.post(target_url + "/api/auth/login", data={"username": "admin", "password": "Admin@123456"})
        if not auth.ok:
            raise AssertionError(f"browser auto-login failed: {auth.status}")
        page.goto(target_url + "/app", wait_until="networkidle")
        for feature_key, page_key, title in PAGES:
            scenario_id = f"scenario-{uuid.uuid4().hex}"
            conn.execute("""INSERT INTO qa_scenario_runs(id,suite_run_id,scenario_key,scenario_version,driver,status,started_at)
              VALUES(?,?,?,?,?,'RUNNING',?)""", (scenario_id, suite_id, feature_key, "mesflow-browser-v1", "BROWSER", now()))
            conn.commit()
            start_errors = (len(page_errors), len(console_errors), len(failed_requests))
            status, first, fingerprint, actual = "PASSED", "", "", {}
            try:
                page.evaluate("([key]) => openPage(key, document.querySelector(`[data-page='${key}']`))", [page_key])
                page.wait_for_timeout(600)
                heading = page.locator("#pageTitle").inner_text()
                if title.lower() not in heading.lower():
                    raise AssertionError(f"page title expected {title!r}, got {heading!r}")
                new_page_errors = page_errors[start_errors[0]:]
                new_console = console_errors[start_errors[1]:]
                new_requests = failed_requests[start_errors[2]:]
                if new_page_errors or new_console or new_requests:
                    raise AssertionError(json.dumps({"page_errors": new_page_errors, "console_errors": new_console,
                                                     "failed_requests": new_requests}, ensure_ascii=False))
                actual = {"page": page_key, "title": heading, "blank": not bool(page.locator("#content").inner_text().strip())}
                if actual["blank"]:
                    raise AssertionError("critical page rendered blank")
            except Exception as exc:
                status, first = "FAILED", "render_and_network_assertions"
                actual = {"error_class": type(exc).__name__, "message": str(exc),
                          "page_errors": page_errors[start_errors[0]:], "console_errors": console_errors[start_errors[1]:],
                          "failed_requests": failed_requests[start_errors[2]:]}
                fingerprint = normalized_fingerprint(feature_key, first, page.url, type(exc).__name__, actual)
                screenshot = evidence_root / run_id / f"failure-{scenario_id}.png"
                screenshot.parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(screenshot), full_page=True)
                evidence.add_file(run_id, screenshot, kind="BROWSER_SCREENSHOT", suite_run_id=suite_id, scenario_run_id=scenario_id)
            ev = evidence.write_json(run_id, f"browser-{page_key}.json", {"scenario": feature_key, "status": status,
              "expected": {"title_contains": title, "no_page_console_or_api_errors": True, "non_blank": True},
              "actual": actual, "first_failing_step": first, "fingerprint": fingerprint},
              kind="BROWSER_EVIDENCE", suite_run_id=suite_id, scenario_run_id=scenario_id)
            conn.execute("""UPDATE qa_scenario_runs SET status=?,finished_at=?,first_failing_step=?,fingerprint=?,
              expected_json=?,actual_json=? WHERE id=?""", (status, now(), first, fingerprint,
              json.dumps({"title_contains": title}), json.dumps(actual), scenario_id))
            conn.execute("INSERT INTO qa_attempts(scenario_run_id,attempt_no,status,fingerprint,started_at,finished_at) VALUES(?,1,?,?,?,?)",
                         (scenario_id, status, fingerprint, now(), now()))
            conn.commit()
            results.append({"key": feature_key, "status": status, "fingerprint": fingerprint, "evidence": ev})
        trace = evidence_root / run_id / "browser-trace.zip"
        context.tracing.stop(path=str(trace))
        context.close(); browser.close()
    evidence.add_file(run_id, trace, kind="PLAYWRIGHT_TRACE", suite_run_id=suite_id)
    har = evidence_root / run_id / "browser.har"
    if har.is_file():
        evidence.add_file(run_id, har, kind="PLAYWRIGHT_HAR", suite_run_id=suite_id)
    suite_status = "FAILED" if any(item["status"] == "FAILED" for item in results) else "PASSED"
    conn.execute("UPDATE qa_suite_runs SET status=?,finished_at=? WHERE id=?", (suite_status, now(), suite_id)); conn.commit()
    return {"suite_id": suite_id, "status": suite_status, "scenarios": results}
