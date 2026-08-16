from __future__ import annotations

import json
import os
import queue
import signal
import sqlite3
import subprocess
import shutil
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import psutil
except ImportError:
    psutil = None
import requests
from flask import Flask, jsonify, make_response, render_template, request

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("MESFLOW_QA_CONFIG_PATH", str(ROOT / "config.json")))
LOG_DIR = Path(os.environ.get("MESFLOW_QA_LOG_DIR", str(ROOT / "logs")))
REPORT_DIR = Path(os.environ.get("MESFLOW_QA_REPORT_DIR", str(ROOT / "reports")))
STATE_DIR = Path(os.environ.get("MESFLOW_QA_STATE_DIR", str(ROOT / "runtime")))
AUTO_RESUME = os.environ.get("MESFLOW_QA_AUTO_RESUME", "1").strip().lower() not in {"0","false","no","off"}
ACTIVE_RUN_FILE = STATE_DIR / "active_run.json"
CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)
REPORT_DIR.mkdir(parents=True, exist_ok=True)
STATE_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
APP_VERSION = "1.20.1"
QA_PROFILE = os.environ.get("MESFLOW_QA_PROFILE", "LOCAL").strip().upper()
if QA_PROFILE not in {"LOCAL", "PRODUCTION_TEST"}:
    raise RuntimeError("MESFLOW_QA_PROFILE must be LOCAL or PRODUCTION_TEST")

_kiosk_lock = threading.RLock()
_kiosk_state: dict[str, dict[str, Any]] = {}


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


QA_INTERNAL_URL = os.environ.get("MESFLOW_QA_INTERNAL_URL", "http://mesflow-app:8080").rstrip("/")
QA_INTERNAL_ONLY = os.environ.get("MESFLOW_QA_INTERNAL_ONLY", "1").strip().lower() not in {"0","false","no","off"}

DEFAULT_CONFIG: dict[str, Any] = {
    "base_url": QA_INTERNAL_URL,
    "internal_base_url": QA_INTERNAL_URL,
    "verify_ssl": False,
    "username": os.environ.get("MESFLOW_QA_USERNAME", "admin"),
    "password": os.environ.get("MESFLOW_QA_PASSWORD", ""),
    "database_path": "",
    "functional_paths": [
        "/login", "/app", "/kiosk", "/api/system/health",
        "/api/execution/health", "/api/dashboard/summary", "/api/auth/me"
    ],
    "browser_paths": ["/login", "/dashboard", "/kiosk"],
    "api_workers": int(os.environ.get("MESFLOW_QA_API_WORKERS", "10")),
    "request_interval_seconds": 2,
    "duration_minutes": int(os.environ.get("MESFLOW_QA_DURATION_MINUTES", "30")),
}

def load_config() -> dict[str, Any]:
    # Windows installer preserves config.json across upgrades. A partially written
    # or older config must never make the dashboard blank.
    base = dict(DEFAULT_CONFIG)
    try:
        if CONFIG_PATH.exists():
            loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict):
                base.update(loaded)
    except Exception as exc:
        print(f"[WARN] config.json invalid, using defaults: {exc}", flush=True)
    if QA_INTERNAL_ONLY:
        base["base_url"] = QA_INTERNAL_URL
        base["internal_base_url"] = QA_INTERNAL_URL
        base["verify_ssl"] = False
    return base


def save_config(data: dict[str, Any]) -> None:
    payload=dict(data)
    if QA_INTERNAL_ONLY:
        payload["base_url"] = QA_INTERNAL_URL
        payload["internal_base_url"] = QA_INTERNAL_URL
        payload["verify_ssl"] = False
    CONFIG_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


@dataclass
class RunState:
    run_id: str
    test_type: str
    status: str = "RUNNING"
    started_at: str = field(default_factory=now)
    ended_at: str | None = None
    total: int = 0
    passed: int = 0
    failed: int = 0
    active_workers: int = 0
    message: str = "Đang khởi động"
    log_file: str = ""
    report_file: str = ""
    pid: int | None = None
    profile: str = ""
    release_version: str = ""
    artifact_digest: str = ""
    stop_event: threading.Event = field(default_factory=threading.Event, repr=False)
    process: subprocess.Popen | None = field(default=None, repr=False)
    log_lines: list[str] = field(default_factory=list, repr=False)

    def public(self) -> dict[str, Any]:
        # Không dùng dataclasses.asdict(): hàm đó deepcopy threading.Event/Popen
        # và gây lỗi "cannot pickle '_thread.lock' object" trên Python 3.13.
        return {
            "run_id": self.run_id,
            "test_type": self.test_type,
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "active_workers": self.active_workers,
            "message": self.message,
            "log_file": self.log_file,
            "report_file": self.report_file,
            "pid": self.pid,
            "profile": self.profile,
            "release_version": self.release_version,
            "artifact_digest": self.artifact_digest,
        }


_lock = threading.RLock()
_runs: dict[str, RunState] = {}


def _write_active_run(data: dict[str, Any] | None) -> None:
    if not data:
        ACTIVE_RUN_FILE.unlink(missing_ok=True)
        return
    tmp = ACTIVE_RUN_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(ACTIVE_RUN_FILE)


def _read_active_run() -> dict[str, Any]:
    try:
        return json.loads(ACTIVE_RUN_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _mark_active_run_finished(state: RunState) -> None:
    active = _read_active_run()
    if active.get("run_id") == state.run_id:
        active["status"] = state.status
        active["ended_at"] = state.ended_at
        active["message"] = state.message
        _write_active_run(active)


def emit(state: RunState, text: str) -> None:
    line = f"[{now()}] {text}"
    with _lock:
        state.log_lines.append(line)
        state.log_lines[:] = state.log_lines[-1000:]
    if state.log_file:
        with open(state.log_file, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def session_from_config(cfg: dict[str, Any]) -> requests.Session:
    s = requests.Session()
    s.verify = bool(cfg.get("verify_ssl", False))
    if not s.verify:
        # Người dùng đã chủ động tắt xác minh SSL; tránh spam InsecureRequestWarning
        # trong soak test. Trạng thái này vẫn được thể hiện trong cấu hình dashboard.
        try:
            requests.packages.urllib3.disable_warnings(
                requests.packages.urllib3.exceptions.InsecureRequestWarning
            )
        except Exception:
            pass
    s.headers.update({"User-Agent": f"MESFlow-QA-Center/{APP_VERSION}", "Accept": "application/json,text/html"})
    username = str(cfg.get("username") or "").strip()
    password = str(cfg.get("password") or "")
    if username:
        if not password:
            raise RuntimeError("QA password is required because production auto-login is disabled")
        response = s.post(str(cfg["base_url"]).rstrip("/") + "/api/auth/login", json={"username": username, "password": password}, timeout=20)
        if response.status_code >= 400:
            raise RuntimeError(f"Đăng nhập thất bại: HTTP {response.status_code} {response.text[:200]}")
        me = s.get(str(cfg["base_url"]).rstrip("/") + "/api/auth/me", timeout=20)
        if me.status_code >= 400:
            raise RuntimeError(f"Đăng nhập không tạo được session: HTTP {me.status_code} {me.text[:200]}")
    return s


def finish(state: RunState, status: str, message: str) -> None:
    with _lock:
        state.status = status
        state.message = message
        state.ended_at = now()
        state.active_workers = 0
    emit(state, message)
    report = state.public()
    report["recent_logs"] = state.log_lines[-200:]
    report_path = REPORT_DIR / f"{state.run_id}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    state.report_file = str(report_path)


def active_runs() -> list[RunState]:
    with _lock:
        return [s for s in _runs.values() if s.status == "RUNNING"]


def database_overview(db_path: str) -> dict[str, Any]:
    path = Path(db_path) if db_path else None
    if not path or not path.exists():
        return {"connected": False, "path": db_path, "error": "Không tìm thấy database"}
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        counts = {}
        preferred = ["production_orders", "product_parts", "operations", "workers", "sessions", "qc_inspections", "devices", "kiosk_stations", "users", "audit_logs"]
        for table in preferred:
            if table in tables:
                try:
                    counts[table] = int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
                except sqlite3.Error:
                    counts[table] = None
        test_counts = {}
        probes = {
            "production_orders": "code", "product_parts": "id", "operations": "id",
            "workers": "id", "devices": "id", "kiosk_stations": "code"
        }
        for table, col in probes.items():
            if table in tables:
                try:
                    test_counts[table] = int(conn.execute(
                        f"SELECT COUNT(*) FROM \"{table}\" WHERE \"{col}\" LIKE 'SIM-%' OR \"{col}\" LIKE 'RTSIM-%' OR \"{col}\" LIKE 'SOAK-%'"
                    ).fetchone()[0])
                except sqlite3.Error:
                    pass
        return {
            "connected": True, "path": str(path), "size_mb": round(path.stat().st_size/1024/1024, 2),
            "modified_at": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
            "integrity": integrity, "table_count": len(tables), "counts": counts, "test_counts": test_counts
        }
    finally:
        conn.close()


def backup_database(db_path: str, reason: str = "manual") -> str:
    src = Path(db_path)
    if not src.exists():
        raise FileNotFoundError(f"Không tìm thấy database: {db_path}")
    backup_dir = ROOT / "backups"
    backup_dir.mkdir(exist_ok=True)
    dst = backup_dir / f"{src.stem}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{reason}.sqlite"
    source = sqlite3.connect(str(src), timeout=30)
    target = sqlite3.connect(str(dst))
    try:
        source.execute("PRAGMA busy_timeout=30000")
        source.backup(target)
    finally:
        target.close(); source.close()
    return str(dst)


def functional_worker(state: RunState, cfg: dict[str, Any]) -> None:
    try:
        s = session_from_config(cfg)
        base = str(cfg["base_url"]).rstrip("/")
        paths = cfg.get("functional_paths") or ["/login", "/dashboard", "/kiosk", "/api/dashboard"]
        for path in paths:
            if state.stop_event.is_set():
                finish(state, "STOPPED", "Đã dừng functional test")
                return
            started = time.perf_counter()
            try:
                r = s.get(base + path, timeout=20, allow_redirects=True)
                elapsed = (time.perf_counter() - started) * 1000
                state.total += 1
                if r.status_code < 400 and not (path != "/login" and r.url.rstrip("/").endswith("/login")):
                    state.passed += 1
                    emit(state, f"PASS {path} HTTP {r.status_code} {elapsed:.0f} ms")
                else:
                    state.failed += 1
                    emit(state, f"FAIL {path} HTTP {r.status_code} -> {r.url}")
            except Exception as exc:
                state.total += 1
                state.failed += 1
                emit(state, f"ERROR {path}: {exc}")
        finish(state, "PASSED" if state.failed == 0 else "FAILED", f"Functional test hoàn tất: {state.passed}/{state.total} đạt")
    except Exception as exc:
        state.failed += 1
        finish(state, "FAILED", f"Functional test lỗi: {exc}")


RELEASE_PROFILES = {
    "release-local": {
        "environment": "LOCAL",
        "required": True,
        "checks": ["auth", "health", "readiness", "dashboard", "kiosk", "system-health"],
    },
    "release-test": {
        "environment": "TEST",
        "required": True,
        "checks": ["auth", "health", "readiness", "dashboard", "kiosk", "exception-center", "production-trace", "system-health"],
    },
}


def release_profile_worker(state: RunState, cfg: dict[str, Any], profile: str) -> None:
    """Small deployed-system release gate. Source/build/migration checks remain
    Deploy Agent facts; QA Center owns only behavior against the target app."""
    try:
        session=session_from_config(cfg);base=str(cfg["base_url"]).rstrip("/")
        endpoints={
            "auth":"/api/auth/me", "health":"/api/system/health", "readiness":"/api/system/ready",
            "dashboard":"/api/dashboard/summary", "kiosk":"/kiosk", "exception-center":"/api/exceptions?limit=1",
            "system-health":"/api/system-health", "production-trace":"/api/production-orders?limit=1",
        }
        checks=[]
        version_response=session.get(base+"/api/system/version",timeout=20)
        actual_version=(version_response.json().get("version") if version_response.ok else "")
        version_ok=actual_version==state.release_version
        checks.append({"name":"version","status":"PASS" if version_ok else "FAIL","required":True,
                       "message":f"expected={state.release_version} actual={actual_version or 'unknown'}"})
        for name in RELEASE_PROFILES[profile]["checks"]:
            started=time.perf_counter()
            try:
                response=session.get(base+endpoints[name],timeout=20,allow_redirects=True)
                ok=response.status_code<400 and not response.url.rstrip("/").endswith("/login")
                checks.append({"name":name,"status":"PASS" if ok else "FAIL","required":True,
                               "message":f"HTTP {response.status_code}","latency_ms":round((time.perf_counter()-started)*1000)})
            except Exception as exc:
                checks.append({"name":name,"status":"ERROR","required":True,"message":f"{type(exc).__name__}: {exc}"})
        state.total=len(checks);state.passed=sum(x["status"]=="PASS" for x in checks);state.failed=state.total-state.passed
        status="PASSED" if state.failed==0 else "FAILED"
        finish(state,status,f"{profile}: {state.passed}/{state.total} required checks PASS")
        report_path=Path(state.report_file);report=json.loads(report_path.read_text(encoding="utf-8"));report["checks"]=checks
        report["profile"]=profile;report["release_version"]=state.release_version;report["artifact_digest"]=state.artifact_digest
        report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    except Exception as exc:
        finish(state,"ERROR",f"{profile} runner error: {type(exc).__name__}: {exc}")


def api_soak_worker(state: RunState, cfg: dict[str, Any], options: dict[str, Any]) -> None:
    duration = max(1, int(options.get("duration_minutes", cfg.get("duration_minutes", 30)))) * 60
    workers = max(1, min(100, int(options.get("workers", cfg.get("api_workers", 10)))))
    interval = max(0.2, float(options.get("interval_seconds", cfg.get("request_interval_seconds", 2))))
    base = str(cfg["base_url"]).rstrip("/")
    paths = ["/api/dashboard", "/api/auth/me", "/kiosk"]
    deadline = time.monotonic() + duration
    state.active_workers = workers

    def client(index: int) -> None:
        try:
            s = session_from_config(cfg)
        except Exception as exc:
            state.failed += 1
            emit(state, f"Worker {index}: login lỗi: {exc}")
            return
        while not state.stop_event.is_set() and time.monotonic() < deadline:
            path = paths[(state.total + index) % len(paths)]
            started = time.perf_counter()
            try:
                r = s.get(base + path, timeout=20, allow_redirects=True)
                elapsed = (time.perf_counter() - started) * 1000
                with _lock:
                    state.total += 1
                    if r.status_code < 400 and not r.url.rstrip("/").endswith("/login"):
                        state.passed += 1
                    else:
                        state.failed += 1
                if r.status_code >= 400:
                    emit(state, f"Worker {index}: FAIL {path} HTTP {r.status_code}")
                elif state.total % 25 == 0:
                    emit(state, f"Tiến độ {state.total} request, lỗi {state.failed}, gần nhất {elapsed:.0f} ms")
            except Exception as exc:
                with _lock:
                    state.total += 1
                    state.failed += 1
                emit(state, f"Worker {index}: {type(exc).__name__}: {exc}")
            state.stop_event.wait(interval)

    try:
        emit(state, f"Bắt đầu API soak: {workers} worker, {duration // 60} phút")
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(client, i + 1) for i in range(workers)]
            for f in futures:
                f.result()
        status = "STOPPED" if state.stop_event.is_set() else ("PASSED" if state.failed == 0 else "COMPLETED_WITH_ERRORS")
        finish(state, status, f"API soak kết thúc: {state.total} request, {state.failed} lỗi")
    except Exception as exc:
        finish(state, "FAILED", f"API soak bị lỗi: {exc}")


def browser_worker(state: RunState, cfg: dict[str, Any], options: dict[str, Any]) -> None:
    browser = None
    context = None
    page = None
    try:
        from playwright.sync_api import sync_playwright
        base = str(cfg["base_url"]).rstrip("/")
        headless = bool(options.get("headless", False))
        loops = max(1, int(options.get("loops", 5)))
        artifact_dir = REPORT_DIR / state.run_id
        artifact_dir.mkdir(parents=True, exist_ok=True)

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless, slow_mo=250 if not headless else 0)
            context = browser.new_context(
                ignore_https_errors=not bool(cfg.get("verify_ssl", False)),
                viewport={"width": 1440, "height": 900},
                record_video_dir=str(artifact_dir / "video"),
            )
            page = context.new_page()
            console_lines: list[str] = []
            page.on("console", lambda msg: console_lines.append(f"{msg.type}: {msg.text}"))
            page.on("pageerror", lambda exc: console_lines.append(f"pageerror: {exc}"))

            page.goto(base + "/login", wait_until="domcontentloaded", timeout=30000)
            emit(state, f"Đã mở trang login: {page.url}")
            username = str(cfg.get("username") or "")
            password = str(cfg.get("password") or "")
            if username:
                user_selector = 'input[name="username"], input[name="email"], #username, #email'
                pass_selector = 'input[name="password"], #password'
                page.locator(user_selector).first.fill(username)
                page.locator(pass_selector).first.fill(password)
                emit(state, "Đã nhập tài khoản và mật khẩu")

                submit_selectors = [
                    'button[type="submit"]',
                    'input[type="submit"]',
                    'button:has-text("Đăng nhập")',
                    'button:has-text("Login")',
                    '#loginButton',
                    '#login-button',
                    '.btn-primary',
                    'form button',
                ]
                clicked = False
                for selector in submit_selectors:
                    locator = page.locator(selector).first
                    try:
                        if locator.count() > 0 and locator.is_visible(timeout=1200) and locator.is_enabled(timeout=1200):
                            emit(state, f"Bấm nút đăng nhập bằng selector: {selector}")
                            locator.click(timeout=5000)
                            clicked = True
                            break
                    except Exception as selector_exc:
                        emit(state, f"Selector không dùng được {selector}: {selector_exc}")

                if not clicked:
                    emit(state, "Không tìm thấy nút đăng nhập, thử nhấn Enter trong ô mật khẩu")
                    page.locator(pass_selector).first.press("Enter")

                try:
                    page.wait_for_url(lambda url: not url.rstrip("/").endswith("/login"), timeout=15000)
                except Exception:
                    page.wait_for_load_state("domcontentloaded", timeout=10000)

                if page.url.rstrip("/").endswith("/login"):
                    raise RuntimeError("Browser login không thành công: vẫn ở trang login sau khi submit")
                emit(state, f"Browser đăng nhập thành công: {page.url}")

            paths = cfg.get("browser_paths") or ["/dashboard", "/kiosk"]
            for loop in range(loops):
                if state.stop_event.is_set():
                    break
                for path in paths:
                    page.goto(base + path, wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(1200)
                    state.total += 1
                    if page.url.rstrip("/").endswith("/login") and path != "/login":
                        state.failed += 1
                        emit(state, f"FAIL Browser {path}: bị chuyển về login")
                    else:
                        state.passed += 1
                        emit(state, f"PASS Browser {path} - {page.title()}")
                    screenshot = artifact_dir / f"loop_{loop+1}_{path.strip('/').replace('/', '_') or 'home'}.png"
                    page.screenshot(path=str(screenshot), full_page=False)

            (artifact_dir / "console.log").write_text("\n".join(console_lines), encoding="utf-8")
            context.close()
            browser.close()
            context = None
            browser = None
        finish(state, "STOPPED" if state.stop_event.is_set() else ("PASSED" if state.failed == 0 else "FAILED"), f"Browser test hoàn tất: {state.passed}/{state.total} đạt. Artifact: {artifact_dir}")
    except Exception as exc:
        artifact_dir = REPORT_DIR / state.run_id
        artifact_dir.mkdir(parents=True, exist_ok=True)
        diagnostics: list[str] = [f"error={type(exc).__name__}: {exc}"]
        if page is not None:
            try:
                diagnostics.append(f"url={page.url}")
            except Exception:
                pass
            try:
                diagnostics.append(f"title={page.title()}")
            except Exception:
                pass
            try:
                page.screenshot(path=str(artifact_dir / "failure.png"), full_page=True)
            except Exception as shot_exc:
                diagnostics.append(f"screenshot_error={shot_exc}")
            try:
                (artifact_dir / "failure.html").write_text(page.content(), encoding="utf-8")
            except Exception as html_exc:
                diagnostics.append(f"html_error={html_exc}")
        (artifact_dir / "failure.txt").write_text("\n".join(diagnostics), encoding="utf-8")
        try:
            if context is not None:
                context.close()
            if browser is not None:
                browser.close()
        except Exception:
            pass
        message = f"Browser test lỗi: {exc}. Đã lưu chẩn đoán tại {artifact_dir}"
        if "Executable doesn't exist" in str(exc) or "browserType.launch" in str(exc):
            message += ". Hãy chạy install_playwright.bat một lần."
        finish(state, "FAILED", message)


def behavioral_worker(state: RunState, cfg: dict[str, Any], options: dict[str, Any]) -> None:
    mode = str(options.get("mode") or "SMOKE").upper()
    if mode not in {"SMOKE", "REGRESSION", "HUMAN_FLOW", "CHAOS", "SOAK", "FULL"}:
        finish(state, "FAILED", f"Execution mode không hợp lệ: {mode}")
        return
    seed = int(options.get("seed", 20260813))
    count = max(1, min(10000, int(options.get("count", 20))))
    cmd = [sys.executable, "-u", str(ROOT / "qa.py"), "run", "--mode", mode, "--seed", str(seed), "--count", str(count)]
    emit(state, f"Sinh behavioral campaign mode={mode}, seed={seed}, count={count}")
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        state.process = proc; state.pid = proc.pid
        assert proc.stdout is not None
        for line in proc.stdout:
            emit(state, line.rstrip())
            if state.stop_event.is_set(): proc.terminate(); break
        code = proc.wait(timeout=15)
        if state.stop_event.is_set(): finish(state, "STOPPED", "Đã dừng behavioral campaign")
        elif code == 0: state.total = count; state.passed = count; finish(state, "PASSED", f"Đã tạo {count} scenario replayable với seed {seed}")
        else: finish(state, "FAILED", f"Behavioral runner exit {code}")
    except Exception as exc:
        finish(state, "FAILED", f"Behavioral runner lỗi: {exc}")


def external_script_worker(state: RunState, cfg: dict[str, Any], test_type: str, options: dict[str, Any]) -> None:
    script = ROOT / "scenarios" / ("realistic_factory_simulation.py" if test_type == "factory_simulation" else "realtime_factory_soak_test.py")
    base = QA_INTERNAL_URL if QA_INTERNAL_ONLY else str(cfg.get("internal_base_url") or cfg["base_url"]).rstrip("/")
    emit(state, f"Kiểm thử MESFlow nội bộ qua Docker: {base}")
    if not script.exists():
        finish(state, "FAILED", f"Thiếu script {script.name}"); return
    if not str(cfg.get("password") or "").strip():
        finish(state,"FAILED","Chưa cấu hình mật khẩu MESFlow; QA không thể chạy hoặc auto-resume bằng production auth")
        return
    cmd = [sys.executable, "-u", str(script), "--base-url", base, "--fallback-base-url", "",
           "--username", str(cfg.get("username") or "admin"),
           "--password", str(cfg.get("password") or ""),
           "--workers", str(max(2, int(options.get("workers", 20)))),
           "--target-active-pos", str(max(2, int(options.get("target_active_pos", 2))))]
    if test_type == "realtime_soak":
        cmd += ["--planned-quantity-min", str(max(500, int(options.get("planned_quantity_min", 500)))),
                "--planned-quantity-max", str(max(500, int(options.get("planned_quantity_max", 1200)))),
                "--run-days", str(max(1, int(options.get("run_days", 7)))),
                "--tick-seconds", str(max(15, int(options.get("tick_seconds", 60)))),
                "--session-target-minutes-min", str(max(120, min(1440, int(options.get("session_target_minutes_min", 120))))),
                "--session-target-minutes-max", str(max(120, min(1440, int(options.get("session_target_minutes_max", 1440))))),
                "--forgot-finish-rate-percent", str(max(0.0, min(20.0, float(options.get("forgot_finish_rate_percent", 4.0))))),
                "--normal-variance-percent", str(max(0, min(30, float(options.get("normal_variance_percent", 30))))),
                "--anomaly-rate-percent", str(max(0, min(20, float(options.get("anomaly_rate_percent", 2))))),
                "--anomaly-multiplier-min", str(max(1.31, float(options.get("anomaly_multiplier_min", 1.8)))),
                "--anomaly-multiplier-max", str(max(1.31, float(options.get("anomaly_multiplier_max", 4.0)))),
                "--fallback-cycle-seconds", str(max(1, float(options.get("fallback_cycle_seconds", 300)))),
                "--batch-qty-min", str(max(1, int(options.get("batch_qty_min", 1)))),
                "--batch-qty-max", str(max(1, int(options.get("batch_qty_max", 5)))),
                "--report-interval-minutes", str(max(5, int(options.get("report_interval_minutes", 60)))),
                "--seed", str(int(options.get("seed", 65817))),
                "--test-run-id", state.run_id]
    else:
        cmd += ["--planned-quantity", str(max(1, int(options.get("planned_quantity", 3))))]
    if bool(cfg.get("verify_ssl", False)): cmd.append("--verify-ssl")
    if options.get("continuous", False) and test_type != "realtime_soak":
        cmd += ["--loop", "--interval-seconds", str(max(60, int(options.get("interval_seconds", 3600))))]
    emit(state, "Chạy: " + " ".join(cmd[:-1] if cfg.get("password") else cmd))
    try:
        env=os.environ.copy(); env.update({"PYTHONUNBUFFERED":"1","PYTHONUTF8":"1","PYTHONIOENCODING":"utf-8"})
        proc=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding="utf-8",errors="replace",env=env)
        state.process=proc; state.pid=proc.pid
        assert proc.stdout is not None
        for line in proc.stdout:
            value=line.rstrip(); emit(state,value); state.total+=1
            upper=value.upper()
            if "[FAIL]" in upper or "TRACEBACK" in upper: state.failed+=1
            elif "[PASS]" in upper: state.passed+=1
            if state.stop_event.is_set(): proc.terminate(); break
        code=proc.wait(timeout=15)
        if state.stop_event.is_set(): finish(state,"STOPPED","Đã dừng mô phỏng")
        elif code==0: finish(state,"PASSED","Test realistic multi-day v65.8.17 kết thúc thành công")
        else: finish(state,"FAILED",f"Test realistic multi-day v65.8.17 lỗi, exit code {code}")
    except Exception as exc: finish(state,"FAILED",f"Không chạy được test: {exc}")

def cleanup_test_data(db_path: str) -> dict[str, int]:
    if not db_path or not Path(db_path).exists():
        raise FileNotFoundError(f"Không tìm thấy database: {db_path}")
    prefixes = ("SIM-%", "RTSIM-%", "SOAK-%")
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    result: dict[str, int] = {}
    try:
        conn.execute("BEGIN IMMEDIATE")
        op_ids = [r[0] for r in conn.execute("SELECT id FROM operations WHERE id LIKE 'SIM-%' OR id LIKE 'RTSIM-%' OR id LIKE 'SOAK-%'")]
        if op_ids:
            marks = ",".join("?" for _ in op_ids)
            for table in ("qc_inspections", "sessions"):
                cur = conn.execute(f"DELETE FROM {table} WHERE operation_id IN ({marks})", op_ids)
                result[table] = cur.rowcount
        statements = {
            "devices": "DELETE FROM devices WHERE id LIKE 'SIM-%' OR id LIKE 'RTSIM-%' OR id LIKE 'SOAK-%'",
            "kiosk_stations": "DELETE FROM kiosk_stations WHERE code LIKE 'SIM-%' OR code LIKE 'RTSIM-%' OR code LIKE 'SOAK-%'",
            "operations": "DELETE FROM operations WHERE id LIKE 'SIM-%' OR id LIKE 'RTSIM-%' OR id LIKE 'SOAK-%'",
            "product_parts": "DELETE FROM product_parts WHERE id LIKE 'SIM-%' OR id LIKE 'RTSIM-%' OR id LIKE 'SOAK-%'",
            "production_orders": "DELETE FROM production_orders WHERE code LIKE 'SIM-%' OR code LIKE 'RTSIM-%' OR code LIKE 'SOAK-%'",
            "workers": "DELETE FROM workers WHERE id LIKE 'SIM-%' OR id LIKE 'RTSIM-%' OR id LIKE 'SOAK-%'",
            "users": "DELETE FROM users WHERE username IN ('sim_supervisor','realtime_supervisor','soak_supervisor')",
            "audit_logs": "DELETE FROM audit_logs WHERE username IN ('sim_supervisor','realtime_supervisor','soak_supervisor') OR actor_worker_id LIKE 'SIM-%' OR actor_worker_id LIKE 'RTSIM-%' OR actor_worker_id LIKE 'SOAK-%' OR device_id LIKE 'SIM-%' OR device_id LIKE 'RTSIM-%' OR device_id LIKE 'SOAK-%'"
        }
        for table, sql in statements.items():
            try:
                cur = conn.execute(sql)
                result[table] = result.get(table, 0) + cur.rowcount
            except sqlite3.OperationalError as exc:
                if "no such table" not in str(exc).lower() and "no such column" not in str(exc).lower():
                    raise
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.get("/")
def index():
    # Inline CSS/JS into the page on Windows. This removes a whole class of
    # blank-page failures caused by /static assets being unavailable/cached.
    try:
        css_text = (ROOT / "static" / "app.css").read_text(encoding="utf-8")
        js_text = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        response = make_response(render_template(
            "index.html", config=load_config(), app_version=APP_VERSION,
            inline_css=css_text, inline_js=js_text,
        ))
    except Exception as exc:
        app.logger.exception("Dashboard render failed")
        safe = str(exc).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        response = make_response(
            f"<!doctype html><meta charset='utf-8'><title>MESFlow QA Center</title>"
            f"<body style='font-family:Segoe UI,sans-serif;padding:32px'>"
            f"<h1>MESFlow QA Center {APP_VERSION}</h1>"
            f"<p>Backend dang chay, nhung dashboard render bi loi.</p>"
            f"<pre style='padding:16px;background:#f3f4f6'>{safe}</pre>"
            f"<p>API version: <a href='/api/version'>/api/version</a></p></body>",
            500, {"Content-Type": "text/html; charset=utf-8"}
        )
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.get("/api/version")
def api_version():
    return jsonify({"ok": True, "app": "MESFlow QA Center", "version": APP_VERSION, "profile": QA_PROFILE, "mode": "internal-only" if QA_INTERNAL_ONLY else "custom", "target": QA_INTERNAL_URL if QA_INTERNAL_ONLY else ""})


@app.get("/kiosk-prototype")
def kiosk_prototype():
    return render_template("kiosk.html")


@app.post("/api/kiosk/heartbeat")
def kiosk_heartbeat():
    payload = request.get_json(force=True) or {}
    kiosk_id = str(payload.get("kiosk_id") or "KIOSK-DEMO-01")
    with _kiosk_lock:
        current = dict(_kiosk_state.get(kiosk_id) or {})
        current.update(payload)
        current["kiosk_id"] = kiosk_id
        current["last_seen"] = now()
        _kiosk_state[kiosk_id] = current
    return jsonify({"ok": True, "state": current})


@app.get("/api/kiosk/status")
def kiosk_status():
    with _kiosk_lock:
        states = list(_kiosk_state.values())
    return jsonify({"kiosks": states})


@app.get("/api/status")
def api_status():
    with _lock:
        runs = [s.public() for s in sorted(_runs.values(), key=lambda x: x.started_at, reverse=True)]
    if psutil is not None:
        cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory()
        system = {"cpu": cpu, "memory_percent": mem.percent, "memory_used_mb": round(mem.used / 1024 / 1024)}
    else:
        system = {"cpu": None, "memory_percent": None, "memory_used_mb": None, "warning": "psutil chưa được cài; System Monitor tạm tắt"}
    return jsonify({"ok": True, "runs": runs, "system": system, "version": APP_VERSION, "mode": "internal-only" if QA_INTERNAL_ONLY else "custom", "target": QA_INTERNAL_URL if QA_INTERNAL_ONLY else ""})


@app.get("/api/release-profiles")
def api_release_profiles():
    return jsonify({"ok":True,"profiles":RELEASE_PROFILES})


@app.post("/api/release-runs")
def api_release_runs():
    payload=request.get_json(force=True) or {};profile=str(payload.get("profile") or "")
    version=str(payload.get("release_version") or "");digest=str(payload.get("artifact_digest") or "")
    if profile not in RELEASE_PROFILES:return jsonify({"ok":False,"error":"UNKNOWN_RELEASE_PROFILE"}),400
    if not version or not __import__('re').fullmatch(r"sha256:[0-9a-f]{64}",digest):return jsonify({"ok":False,"error":"INVALID_RELEASE_IDENTITY"}),400
    with _lock:
        duplicate=next((x for x in _runs.values() if x.status=="RUNNING" and x.profile==profile and x.artifact_digest==digest),None)
        if duplicate:return jsonify({"ok":False,"error":"RELEASE_RUN_ALREADY_ACTIVE","run":duplicate.public()}),409
        run_id=f"{RELEASE_PROFILES[profile]['environment']}-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
        state=RunState(run_id=run_id,test_type="release_gate",profile=profile,release_version=version,artifact_digest=digest)
        state.log_file=str(LOG_DIR/f"{run_id}.log");_runs[run_id]=state
    threading.Thread(target=release_profile_worker,args=(state,load_config(),profile),daemon=True).start()
    return jsonify({"ok":True,"run":state.public()}),202


@app.get("/api/runs/<run_id>/logs")
def api_logs(run_id: str):
    state = _runs.get(run_id)
    if not state:
        return jsonify({"error": "Không tìm thấy run"}), 404
    return jsonify({"lines": state.log_lines[-500:]})


@app.get("/api/runs/<run_id>")
def api_run(run_id: str):
    state=_runs.get(run_id)
    if not state:return jsonify({"ok":False,"error":"RUN_NOT_FOUND"}),404
    result=state.public()
    if state.report_file and Path(state.report_file).is_file():
        try:result.update(json.loads(Path(state.report_file).read_text(encoding="utf-8")))
        except (OSError,json.JSONDecodeError):pass
    return jsonify({"ok":True,"run":result})


@app.post("/api/config")
def api_config():
    data = request.get_json(force=True)
    current = load_config()
    current.update(data)
    save_config(current)
    return jsonify({"ok": True, "config": current})


@app.post("/api/start")
def api_start():
    payload = request.get_json(force=True)
    test_type = str(payload.get("test_type") or "functional")
    allowed = {"functional", "api_soak", "browser_visual", "behavioral", "factory_simulation", "realtime_soak"}
    if test_type not in allowed:
        return jsonify({"error": "Loại test không hợp lệ"}), 400
    cfg = load_config()
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    state = RunState(run_id=run_id, test_type=test_type)
    state.log_file = str(LOG_DIR / f"{run_id}.log")
    _runs[run_id] = state
    if test_type == "realtime_soak":
        _write_active_run({
            "run_id": run_id,
            "test_type": test_type,
            "status": "RUNNING",
            "started_at": state.started_at,
            "payload": payload,
            "auto_resume": True,
        })
    target = {
        "functional": functional_worker,
        "api_soak": api_soak_worker,
        "browser_visual": browser_worker,
        "behavioral": behavioral_worker,
    }.get(test_type)
    if target:
        args = (state, cfg) if test_type == "functional" else (state, cfg, payload)
        threading.Thread(target=target, args=args, daemon=True).start()
    else:
        threading.Thread(target=external_script_worker, args=(state, cfg, test_type, payload), daemon=True).start()
    return jsonify({"ok": True, "run": state.public()})


@app.post("/api/stop/<run_id>")
def api_stop(run_id: str):
    state = _runs.get(run_id)
    if not state:
        return jsonify({"error": "Không tìm thấy run"}), 404
    state.stop_event.set()
    active = _read_active_run()
    if active.get("run_id") == run_id:
        active["auto_resume"] = False
        active["status"] = "STOP_REQUESTED"
        _write_active_run(active)
    if state.process and state.process.poll() is None:
        try:
            state.process.terminate()
        except Exception:
            pass
    state.message = "Đang dừng..."
    return jsonify({"ok": True})


@app.post("/api/cleanup")
def api_cleanup():
    """Delete only QA-owned records through MESFlow public APIs.

    Safety boundary:
    - Production Orders whose code starts with QAV65817-
    - Employees whose code starts with QAV65817-EMP-
    No direct database access and no deletion of normal factory records.
    """
    if active_runs():
        return jsonify({"ok": False, "error": "Không thể xóa dữ liệu khi test đang chạy"}), 409

    payload = request.get_json(silent=True) or {}
    confirm = str(payload.get("confirm") or "").strip().upper()
    preview_only = bool(payload.get("preview", False))
    if not preview_only and confirm != "DELETE QA DATA":
        return jsonify({"ok": False, "error": "CONFIRMATION_REQUIRED", "message": "Nhập chính xác DELETE QA DATA để xác nhận"}), 400

    cfg = load_config()
    base = QA_INTERNAL_URL if QA_INTERNAL_ONLY else str(cfg.get("internal_base_url") or cfg.get("base_url") or "").rstrip("/")
    try:
        local_cfg = dict(cfg)
        local_cfg["base_url"] = base
        clients = [(base, session_from_config(local_cfg))]

        def request_api(method: str, path: str, **kwargs):
            last = None
            timeout = kwargs.pop("timeout", 45)
            for base, client in clients:
                try:
                    response = getattr(client, method)(base + path, timeout=timeout, **kwargs)
                    last = response
                    if response.status_code not in {429,502,503,504,521,522,523,524}:
                        return response
                except Exception as exc:
                    last = exc
            if hasattr(last, "status_code"):
                return last
            raise RuntimeError(f"Không gọi được API cleanup: {last}")

        def get_items(path: str) -> list[dict[str, Any]]:
            response = request_api("get", path, timeout=30)
            if response.status_code >= 400:
                raise RuntimeError(f"GET {path}: HTTP {response.status_code} {response.text[:300]}")
            body = response.json()
            return list(body.get("items") or [])

        production_orders = [
            item for item in get_items("/api/production-orders?limit=1000")
            if str(item.get("code") or "").strip().upper().startswith(("QAV65817-","QAV65813-"))
        ]
        employees = [
            item for item in get_items("/api/employees?limit=1000")
            if str(item.get("employee_no") or item.get("code") or "").strip().upper().startswith(("QAV65817-EMP-","QAV65813-EMP-"))
        ]
        stations = [
            item for item in get_items("/api/stations?limit=1000")
            if str(item.get("code") or "").strip().upper().startswith(("QAV65817-ST-","QAV65813-ST-"))
        ]
        preview = {
            "production_orders": [{"id": x.get("id"), "code": x.get("code"), "status": x.get("status")} for x in production_orders],
            "employees": [{"id": x.get("id"), "code": x.get("employee_no") or x.get("code"), "name": x.get("name")} for x in employees],
            "stations": [{"id": x.get("id"), "code": x.get("code"), "name": x.get("name")} for x in stations],
        }
        if preview_only:
            return jsonify({"ok": True, "preview": preview, "counts": {"production_orders": len(production_orders), "employees": len(employees), "stations": len(stations)}})

        result: dict[str, Any] = {
            "deleted_production_orders": [],
            "deleted_employees": [],
            "deleted_stations": [],
            "failed": [],
        }
        # Delete POs first so sessions and execution references are removed by the
        # backend's force-delete transaction before deleting QA employees.
        for po in production_orders:
            po_id = po.get("id")
            code = str(po.get("code") or "")
            try:
                response = request_api("delete", f"/api/production-orders/{po_id}/force", json={"confirm_code": code}, timeout=45)
                body = response.json() if response.content else {}
                if response.status_code >= 400 or body.get("ok") is False:
                    raise RuntimeError(f"HTTP {response.status_code} {body or response.text[:300]}")
                result["deleted_production_orders"].append({"id": po_id, "code": code, "counts": body.get("counts") or {}})
            except Exception as exc:
                result["failed"].append({"type": "production_order", "id": po_id, "code": code, "error": str(exc)})

        for employee in employees:
            employee_id = employee.get("id")
            code = str(employee.get("employee_no") or employee.get("code") or "")
            try:
                response = request_api("delete", f"/api/employees/{employee_id}", timeout=30)
                body = response.json() if response.content else {}
                if response.status_code >= 400 or body.get("ok") is False:
                    raise RuntimeError(f"HTTP {response.status_code} {body or response.text[:300]}")
                result["deleted_employees"].append({"id": employee_id, "code": code})
            except Exception as exc:
                result["failed"].append({"type": "employee", "id": employee_id, "code": code, "error": str(exc)})

        # Xóa trạm QA sau cùng. Nếu kiosk identity còn tham chiếu, backend sẽ từ chối;
        # ghi rõ failed thay vì đụng dữ liệu ngoài API.
        for station in stations:
            station_id = station.get("id")
            code = str(station.get("code") or "")
            try:
                response = request_api("delete", f"/api/stations/{station_id}", timeout=30)
                body = response.json() if response.content else {}
                if response.status_code >= 400 or body.get("ok") is False:
                    raise RuntimeError(f"HTTP {response.status_code} {body or response.text[:300]}")
                result["deleted_stations"].append({"id": station_id, "code": code})
            except Exception as exc:
                result["failed"].append({"type": "station", "id": station_id, "code": code, "error": str(exc)})

        return jsonify({
            "ok": True,
            "partial": len(result["failed"]) > 0,
            "result": result,
            "counts": {
                "deleted_production_orders": len(result["deleted_production_orders"]),
                "deleted_employees": len(result["deleted_employees"]),
                "deleted_stations": len(result["deleted_stations"]),
                "failed": len(result["failed"]),
            },
        }), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500


@app.post("/api/database/backup")
def api_database_backup():
    cfg = load_config()
    try:
        path = backup_database(str(cfg.get("database_path") or ""), "manual")
        return jsonify({"ok": True, "path": path})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.get("/api/database/overview")
def api_database_overview():
    cfg = load_config()
    return jsonify(database_overview(str(cfg.get("database_path") or "")))


@app.post("/api/check-connection")
def check_connection():
    cfg = load_config()
    try:
        s = session_from_config(cfg)
        r = s.get(str(cfg["base_url"]).rstrip("/") + "/api/auth/me", timeout=15)
        return jsonify({"ok": r.status_code < 400, "status": r.status_code, "url": r.url})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


def _resume_persistent_run_after_startup():
    if not AUTO_RESUME:
        return
    spec = _read_active_run()
    if not spec or spec.get("test_type") != "realtime_soak":
        return
    if spec.get("status") not in {"RUNNING", "WAITING_MESFLOW", "INTERRUPTED"}:
        return
    if spec.get("auto_resume") is False:
        return

    def resume():
        time.sleep(3)
        run_id = str(spec.get("run_id") or (datetime.now().strftime("%Y%m%d-%H%M%S")+"-resume"))
        state = RunState(
            run_id=run_id,
            test_type="realtime_soak",
            status="RUNNING",
            started_at=str(spec.get("started_at") or now()),
            message="Tự động tiếp tục sau khi QA Center restart/deploy",
        )
        state.log_file = str(LOG_DIR / f"{run_id}.log")
        with _lock:
            _runs[run_id] = state
        emit(state, "[RESUME] QA Center vừa restart/deploy; tiếp tục realistic multi-day từ persistent state")
        cfg = load_config()
        external_script_worker(state, cfg, "realtime_soak", dict(spec.get("payload") or {}))

    threading.Thread(target=resume, daemon=True).start()


@app.errorhandler(Exception)
def handle_unexpected_error(exc):
    app.logger.exception("Unhandled QA Center error")
    return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500


if __name__ == "__main__":
    host = os.environ.get("MESFLOW_QA_HOST", "127.0.0.1")
    port = int(os.environ.get("MESFLOW_QA_PORT", "8095"))
    print(f"MESFlow QA Center V{APP_VERSION}: http://{host}:{port}", flush=True)
    _resume_persistent_run_after_startup()
    app.run(host=host, port=port, debug=False, threaded=True)
