"""UI Coverage run = bug discovery run (requirement 5).

Drives a READY preview environment with a real browser (reusing the same
Playwright pattern as agent.py's existing Browser Visual worker), collects
console errors / pageerrors / failed network requests / unexpected HTTP
status / suspicious rendered text, cross-checks a couple of dashboard API
responses against the seed's expected-state manifest, and turns every
finding into a structured, deduped bug record via ``bug_store``.

Never hides a real MESFlow problem to keep a run "green" -- every finding
becomes a bug record; this module's own return value is a summary, not a
pass/fail verdict.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import requests

from . import bug_store
from . import evidence as evidence_mod
from . import qa_store

REPORT_DIR = Path(os.environ.get("MESFLOW_QA_REPORT_DIR", "/data/reports")) / "coverage"

BANNED_SUBSTRINGS = [
    "Unable to process the request",
    "Internal Server Error",
    "Traceback (most recent call last)",
    "undefined",
    "NaN",
]
# Checked in *rendered visible text* (Playwright's innerText), never raw HTML
# source -- raw HTML also contains <script> bodies where "undefined"/"NaN"
# are ordinary, harmless JS keywords.

BLANK_PAGE_MIN_CHARS = 40


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class _Findings:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def add(
        self, *, feature: str, error_type: str, title: str, severity: str = "MEDIUM",
        expected: Any = "", actual: Any = "", url: str = "", api: str = "",
        console_log: list[str] | None = None, pageerror: str = "",
        response_status: int | None = None, response_body_excerpt: str = "",
        screenshot_path: str = "",
    ) -> None:
        self.items.append({
            "feature": feature, "error_type": error_type, "title": title, "severity": severity,
            "expected": expected, "actual": actual, "url": url, "api": api,
            "console_log": console_log or [], "pageerror": pageerror,
            "response_status": response_status, "response_body_excerpt": response_body_excerpt,
            "screenshot_path": screenshot_path,
        })


def _capture_screenshot(page, run_id: str, name: str) -> str:
    """Best-effort evidence screenshot (requirement 15). Returns the API path
    to fetch it, or '' if the capture itself failed -- a broken screenshot
    must never abort a coverage run."""
    try:
        run_dir = REPORT_DIR / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(run_dir / f"{name}.png"), full_page=True)
        return f"/api/preview/coverage/{run_id}/screenshot/{name}.png"
    except Exception:  # noqa: BLE001 - evidence is best-effort, never fatal
        return ""


def _check_rendered_page(page, feature: str, url: str, findings: _Findings, screenshot_path: str = "") -> None:
    try:
        text = page.inner_text("body")
    except Exception as exc:  # noqa: BLE001
        findings.add(feature=feature, error_type="RENDER_ERROR", title=f"Could not read rendered body: {exc}",
                     severity="HIGH", url=url, screenshot_path=screenshot_path)
        return
    stripped = text.strip()
    if len(stripped) < BLANK_PAGE_MIN_CHARS:
        findings.add(feature=feature, error_type="BLANK_PAGE",
                     title=f"Page at {url} rendered almost no visible text ({len(stripped)} chars)",
                     severity="HIGH", url=url, actual=stripped[:200], screenshot_path=screenshot_path)
    for needle in BANNED_SUBSTRINGS:
        if needle in text:
            findings.add(feature=feature, error_type="ERROR_TEXT_ON_PAGE",
                         title=f"Page contains banned text: {needle!r}", severity="HIGH",
                         url=url, actual=text[:500], screenshot_path=screenshot_path)


def _check_apis(base_url: str, env: dict[str, Any], manifest: dict[str, Any], findings: _Findings) -> None:
    session = requests.Session()
    login_url = f"{base_url}/api/auth/login"
    try:
        resp = session.post(login_url, json={"username": "admin", "password": env["admin_password"]}, timeout=15)
    except Exception as exc:  # noqa: BLE001
        findings.add(feature="auth.login", error_type="LOGIN_FAILED", title=f"POST /api/auth/login raised: {exc}",
                     severity="HIGH", api=login_url)
        return
    if resp.status_code != 200 or not resp.json().get("ok"):
        findings.add(feature="auth.login", error_type="LOGIN_FAILED",
                     title=f"POST /api/auth/login returned {resp.status_code}", severity="HIGH",
                     api=login_url, response_status=resp.status_code, response_body_excerpt=resp.text[:500])
        return

    exp = (manifest or {}).get("expectations") or {}

    def get(path: str) -> requests.Response | None:
        try:
            r = session.get(f"{base_url}{path}", timeout=15)
        except Exception as exc:  # noqa: BLE001
            findings.add(feature="dashboard.production_overview", error_type="API_ERROR",
                         title=f"GET {path} raised: {exc}", severity="HIGH", api=path)
            return None
        if r.status_code >= 400:
            findings.add(feature="dashboard.production_overview", error_type="HTTP_ERROR",
                         title=f"GET {path} returned unexpected {r.status_code}", severity="HIGH",
                         api=path, response_status=r.status_code, response_body_excerpt=r.text[:500])
            return None
        try:
            body = r.json()
        except ValueError:
            findings.add(feature="dashboard.production_overview", error_type="NON_JSON_RESPONSE",
                         title=f"GET {path} did not return JSON", severity="MEDIUM", api=path,
                         response_status=r.status_code, response_body_excerpt=r.text[:300])
            return None
        if body.get("ok") is False:
            findings.add(feature="dashboard.production_overview", error_type="API_NOT_OK",
                         title=f"GET {path} returned ok=false", severity="HIGH", api=path,
                         response_status=r.status_code, response_body_excerpt=json.dumps(body)[:500])
            return None
        return r

    summary_resp = get("/api/dashboard/summary")
    sessions_expected = (exp.get("sessions") or {}).get("open")
    if summary_resp is not None and sessions_expected is not None:
        active_sessions = (summary_resp.json().get("summary") or {}).get("active_sessions")
        if active_sessions is None or active_sessions < sessions_expected:
            findings.add(
                feature="dashboard.production_overview", error_type="REPORT_MISMATCH",
                title="Dashboard active_sessions is lower than what the seed guarantees",
                severity="HIGH", api="/api/dashboard/summary",
                expected=f">= {sessions_expected}", actual=active_sessions,
            )

    po_resp = get("/api/dashboard/production-orders")
    po_expected = exp.get("production_orders") or {}
    if po_resp is not None and po_expected:
        items = po_resp.json().get("items") or []
        today = datetime.now().date()
        late = sum(1 for it in items if str(it.get("status")) == "IN_PROGRESS" and it.get("due_date") and _as_date(it["due_date"]) and _as_date(it["due_date"]) < today)
        completed = sum(1 for it in items if str(it.get("status")) == "COMPLETED")
        if po_expected.get("late") and late < po_expected["late"]:
            findings.add(feature="dashboard.production_overview", error_type="REPORT_MISMATCH",
                         title="Fewer late production orders visible on the dashboard than the seed guarantees",
                         severity="HIGH", api="/api/dashboard/production-orders",
                         expected=f">= {po_expected['late']}", actual=late)
        if po_expected.get("completed") and completed < po_expected["completed"]:
            findings.add(feature="dashboard.production_overview", error_type="REPORT_MISMATCH",
                         title="Fewer completed production orders visible on the dashboard than the seed guarantees",
                         severity="MEDIUM", api="/api/dashboard/production-orders",
                         expected=f">= {po_expected['completed']}", actual=completed)

    exc_expected = (exp.get("exceptions") or {}).get("minimum")
    if exc_expected:
        exc_resp = get("/api/session-exceptions")
        if exc_resp is not None:
            count = len(exc_resp.json().get("items") or [])
            if count < exc_expected:
                findings.add(feature="dashboard.production_overview", error_type="REPORT_MISMATCH",
                             title="Fewer open session exceptions visible than the seed guarantees",
                             severity="MEDIUM", api="/api/session-exceptions",
                             expected=f">= {exc_expected}", actual=count)

    _check_factory_scale(get, exp.get("scale") or {}, findings)
    _check_productivity(get, exp.get("scale") or {}, (manifest or {}).get("anchor") or "", findings)


def _check_factory_scale(get, scale_expected: dict[str, Any], findings: _Findings) -> None:
    """Assert the factory-scale expectations (FULL_UI): not just "did the
    page load", but did MESFlow actually SHOW the employee count, the
    active-session count, and the progress-band distribution the seed just
    put in the database (requirement: "UI Coverage phải assert các
    expectation trên, không chỉ page load"). `get` is _check_apis's own
    authenticated GET helper -- every failure it hits already becomes a
    finding on its own, so this only adds findings for what it can compare."""
    if not scale_expected:
        return

    employees_expected = scale_expected.get("employees")
    if employees_expected:
        resp = get("/api/employees")
        if resp is not None:
            count = len(resp.json().get("items") or [])
            if count < employees_expected:
                findings.add(feature="employees", error_type="REPORT_MISMATCH",
                             title="Fewer employees visible than the seed guarantees",
                             severity="HIGH", api="/api/employees",
                             expected=f">= {employees_expected}", actual=count)

    active_expected = scale_expected.get("active_sessions_min")
    if active_expected:
        resp = get("/api/dashboard/active-sessions")
        if resp is not None:
            count = len(resp.json().get("items") or [])
            if count < active_expected:
                findings.add(feature="dashboard.production_overview", error_type="REPORT_MISMATCH",
                             title="Fewer active sessions visible than the seed guarantees",
                             severity="HIGH", api="/api/dashboard/active-sessions",
                             expected=f">= {active_expected}", actual=count)

    partial_expected = scale_expected.get("operations_with_partial_progress_min")
    completed_expected = scale_expected.get("operations_completed_min")
    if partial_expected or completed_expected:
        resp = get("/api/kpi/operations?limit=500")
        if resp is not None:
            ops = resp.json().get("items") or []
            partial = sum(1 for o in ops if 0 < float(o.get("completion_percent") or 0) < 100)
            completed = sum(1 for o in ops if float(o.get("completion_percent") or 0) >= 100)
            if partial_expected and partial < partial_expected:
                findings.add(feature="operations", error_type="REPORT_MISMATCH",
                             title="Fewer operations with partial progress than the seed guarantees",
                             severity="MEDIUM", api="/api/kpi/operations",
                             expected=f">= {partial_expected}", actual=partial)
            if completed_expected and completed < completed_expected:
                findings.add(feature="operations", error_type="REPORT_MISMATCH",
                             title="Fewer fully-completed operations than the seed guarantees",
                             severity="MEDIUM", api="/api/kpi/operations",
                             expected=f">= {completed_expected}", actual=completed)
            # Cross-check operation aggregate vs its own session history --
            # the seed keeps these identical by construction; if MESFlow's
            # own KPI query ever disagrees with what was actually seeded,
            # that's a real backend bug, not a coverage false positive.
            for o in ops:
                plan = float(o.get("plan_qty") or 0)
                done = float(o.get("done_qty") or 0)
                reported_pct = float(o.get("completion_percent") or 0)
                if plan > 0:
                    expected_pct = round(done / plan * 100, 2)
                    if abs(expected_pct - reported_pct) > 0.5:
                        findings.add(
                            feature="operations", error_type="REPORT_MISMATCH",
                            title=f"Operation {o.get('code')}: completion_percent disagrees with done_qty/plan_qty",
                            severity="HIGH", api="/api/kpi/operations",
                            expected=expected_pct, actual=reported_pct,
                        )


def _check_productivity(get, scale_expected: dict[str, Any], anchor_iso: str, findings: _Findings) -> None:
    """The regression this preset exists for (requirement: "UI Coverage
    phải detect trường hợp: completed_sessions > 0 AND productivity_valid_
    sessions = 0 -> FAIL"). There is no dedicated
    /api/reports/employee-productivity endpoint in MESFlow -- the real,
    equivalent feature is /api/reports/employee-performance, whose
    summary.efficiency_percent is exactly expected_seconds/actual_seconds*100
    (see mesflow/db/repositories/analytics.py ReportRepository.
    employee_performance). Called with no employee_id it aggregates every
    employee's sessions in range into one summary -- that IS the
    "productivity report" the task describes, just named efficiency."""
    history_days = scale_expected.get("history_days")
    if not history_days:
        return
    try:
        anchor_date = datetime.fromisoformat(anchor_iso).date() if anchor_iso else datetime.now().date()
    except ValueError:
        anchor_date = datetime.now().date()
    # +1 day of headroom: the seed's day_offset can be up to history_days
    # PLUS an hour/minute jitter on top (a session dated "history_days days
    # ago, 9am" is still within the promised window but sorts before
    # midnight on history_days-days-ago) -- the report API filters by
    # `started_at >= from::date` (date-only), so a too-tight boundary here
    # undercounts sessions that were genuinely seeded inside the window.
    # Found live: without this, a real seed of 319 valid sessions reported
    # only 313 through this filter.
    date_from = (anchor_date - timedelta(days=int(history_days) + 1)).isoformat()
    date_to = anchor_date.isoformat()
    api = f"/api/reports/employee-performance?from={date_from}&to={date_to}"
    resp = get(api)
    if resp is None:
        return
    # The real response nests everything under "report" (see
    # mesflow/web/analytics.py employee_performance_report:
    # `jsonify(ok=True, report=report)`) -- found live, missed on first
    # write since every other endpoint this module calls returns its
    # payload flat.
    report = resp.json().get("report") or {}
    employees = report.get("employees") or []
    sessions = report.get("sessions") or []
    summary = report.get("summary") or {}

    if not employees:
        findings.add(feature="reports.employee_performance", error_type="REPORT_MISMATCH",
                     title="Employee performance report returned no employees", severity="HIGH", api=api)

    completed = int(summary.get("completed_session_count") or 0)
    valid_expected = scale_expected.get("productivity_valid_sessions")
    if valid_expected:
        # The exact historical bug this preset guards against: sessions
        # exist, but every single one is missing standard_seconds_per_unit,
        # so efficiency_percent can never be computed for any of them.
        if completed > 0 and summary.get("efficiency_percent") is None:
            findings.add(
                feature="reports.employee_performance", error_type="PRODUCTIVITY_ALWAYS_NULL",
                title="completed_session_count > 0 but efficiency_percent is null for the whole report "
                      "(every session is missing operation.standard_seconds_per_unit)",
                severity="HIGH", api=api, expected="efficiency_percent != null", actual=None,
            )
        reported_with_qty = sum(1 for s in sessions if (int(s.get("good_qty") or 0) + int(s.get("defect_qty") or 0)) > 0
                                 and float(s.get("standard_seconds_per_unit") or 0) > 0 and s.get("status") != "OPEN")
        if reported_with_qty < valid_expected:
            findings.add(feature="reports.employee_performance", error_type="REPORT_MISMATCH",
                         title="Fewer productivity-valid sessions visible than the seed guarantees",
                         severity="HIGH", api=api, expected=f">= {valid_expected}", actual=reported_with_qty)

    pct_range = scale_expected.get("avg_productivity_percent_expected_range")
    if pct_range and summary.get("efficiency_percent") is not None:
        lo, hi = pct_range
        eff = float(summary["efficiency_percent"])
        # Generous margin: this is the *overall* aggregate across every
        # session, not the same per-employee-average the range was tuned
        # against -- only flag it if it's wildly outside, a real signal
        # something is broken, not a false positive on a plausible spread.
        if eff < lo - 20 or eff > hi + 30:
            findings.add(feature="reports.employee_performance", error_type="REPORT_MISMATCH",
                         title="Overall efficiency_percent is far outside the expected productivity range",
                         severity="MEDIUM", api=api, expected=pct_range, actual=eff)


def _as_date(value: Any):
    """Parse whatever date shape the dashboard API returns. Found live: the
    real API serializes due_date as an RFC 1123 HTTP-date
    ("Thu, 20 Aug 2026 00:00:00 GMT"), not ISO 8601 -- the original
    ISO-only parser silently treated every date as unparseable, so
    REPORT_MISMATCH never fired even when the count genuinely was wrong."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return parsedate_to_datetime(text).date()
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(text[:10]).date()
    except ValueError:
        return None


def run_coverage(
    preview_manager, env_id: str, *, headless: bool = True, qa_center_version: str = "",
) -> dict[str, Any]:
    env = preview_manager.get(env_id)
    if env["status"] != "READY":
        raise RuntimeError(f"Preview {env_id} is not READY (status={env['status']})")
    base_url = preview_manager.internal_base_url(env_id)
    manifest = env.get("manifest") or {}
    run_id = f"cov-{uuid.uuid4().hex[:10]}"
    started_at = _now()
    conn = qa_store.connect()
    conn.execute(
        "INSERT INTO coverage_runs(run_id,preview_id,preset,started_at,status) VALUES(?,?,?,?,?)",
        (run_id, env_id, env["preset"], started_at, "RUNNING"),
    )
    conn.commit()

    findings = _Findings()
    checks: list[dict[str, Any]] = []

    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError:
        sync_playwright = None
        PlaywrightTimeoutError = Exception

    if sync_playwright is None:
        checks.append({"check": "playwright_available", "ok": False})
    else:
        console_errors: list[str] = []
        pageerrors: list[str] = []
        failed_requests: list[dict[str, Any]] = []
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless)
            context = browser.new_context(ignore_https_errors=True, viewport={"width": 1440, "height": 900})
            page = context.new_page()
            page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
            page.on("pageerror", lambda exc: pageerrors.append(str(exc)))
            page.on("response", lambda resp: failed_requests.append({"url": resp.url, "status": resp.status})
                    if resp.status >= 400 else None)

            login_url = f"{base_url}/login"
            resp = page.goto(login_url, wait_until="domcontentloaded", timeout=30000)
            checks.append({"check": "login_page_reachable", "status": resp.status if resp else None})
            login_shot = _capture_screenshot(page, run_id, "01-login")
            _check_rendered_page(page, "auth.login", login_url, findings, screenshot_path=login_shot)

            try:
                page.locator('input[name="username"], #username').first.fill("admin")
                page.locator('input[name="password"], #password').first.fill(env["admin_password"])
                page.locator('button[type="submit"], form button').first.click(timeout=5000)
                # MESFlow's login page submits via fetch() + location.href on
                # success (see static/login.js), not a normal <form> POST --
                # wait_for_load_state("domcontentloaded") can spuriously
                # return immediately against the *pre-navigation* page before
                # the async fetch even resolves, so the "still on /login"
                # check below would misfire on a login that actually
                # succeeded a moment later. Wait for the URL itself to
                # change instead; a real credential failure still times out
                # here and falls through to that same check correctly.
                try:
                    page.wait_for_url(lambda u: not u.rstrip("/").endswith("/login"), timeout=15000)
                except PlaywrightTimeoutError:
                    pass
            except Exception as exc:  # noqa: BLE001
                findings.add(feature="auth.login", error_type="LOGIN_FORM_ERROR",
                             title=f"Could not submit the login form: {exc}", severity="HIGH", url=login_url)

            app_shot = ""
            if page.url.rstrip("/").endswith("/login"):
                findings.add(feature="auth.login", error_type="UNEXPECTED_LOGIN_REDIRECT",
                             title="Still on /login after submitting valid preview admin credentials",
                             severity="HIGH", url=page.url)
            else:
                app_url = f"{base_url}/app"
                resp = page.goto(app_url, wait_until="networkidle", timeout=30000)
                checks.append({"check": "app_page_reachable", "status": resp.status if resp else None})
                page.wait_for_timeout(800)
                app_shot = _capture_screenshot(page, run_id, "02-app")
                _check_rendered_page(page, "dashboard.production_overview", app_url, findings, screenshot_path=app_shot)

            browser.close()

        for msg in console_errors:
            findings.add(feature="global.browser", error_type="CONSOLE_ERROR", title=msg, severity="MEDIUM",
                         url=f"{base_url}/app", console_log=[msg], screenshot_path=app_shot)
        for msg in pageerrors:
            findings.add(feature="global.browser", error_type="PAGEERROR", title=msg, severity="HIGH",
                         url=f"{base_url}/app", pageerror=msg, screenshot_path=app_shot)
        for item in failed_requests:
            findings.add(feature="global.network", error_type="FAILED_REQUEST",
                         title=f"{item['status']} {item['url']}", severity="MEDIUM",
                         api=item["url"], response_status=item["status"])

    _check_apis(base_url, env, manifest, findings)

    bug_ids: list[str] = []
    for f in findings.items:
        ev = evidence_mod.build_evidence(
            feature=f["feature"], scenario=env["preset"], seed_version=str(env.get("seed_version", "")),
            mesflow_version=str(env.get("mesflow_version", "")), qa_center_version=qa_center_version,
            url=f["url"], api=f["api"], response_status=f["response_status"],
            response_body_excerpt=f["response_body_excerpt"], console_log=f["console_log"],
            pageerror=f["pageerror"], expected=f["expected"], actual=f["actual"],
            screenshot_path=f["screenshot_path"],
        )
        bug = bug_store.record_bug(
            feature=f["feature"], error_type=f["error_type"], title=f["title"], severity=f["severity"],
            expected=str(f["expected"]), actual=str(f["actual"]), url=f["url"], api=f["api"],
            evidence=ev, run_id=run_id,
        )
        bug_ids.append(bug["bug_id"])

    finished_at = _now()
    summary = {
        "run_id": run_id, "preview_id": env_id, "preset": env["preset"],
        "started_at": started_at, "finished_at": finished_at,
        "bug_count": len(bug_ids), "bug_ids": bug_ids, "checks": checks,
    }
    conn.execute(
        "UPDATE coverage_runs SET finished_at=?,status=?,bug_count=?,checks_json=?,summary_json=? WHERE run_id=?",
        (finished_at, "DONE", len(bug_ids), json.dumps(checks), json.dumps(summary, default=str), run_id),
    )
    conn.commit()
    return summary
