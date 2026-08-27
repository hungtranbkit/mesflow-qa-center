from __future__ import annotations

import json
import os
import platform
import queue
import re
import signal
import sqlite3
import subprocess
import shutil
import sys
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, unquote

try:
    import psutil
except ImportError:
    psutil = None
import requests
try:
    import psycopg
    from psycopg import sql
except ImportError:
    psycopg = None
    sql = None
from flask import Flask, jsonify, make_response, render_template, request, send_file

from engine import bug_store, coverage_runner, feature_registry, qa_store, regression_policy
from engine import preview_manager as preview_manager_mod
from engine.preview import presets as preview_presets
from qualification.api import bp as qualification_bp

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
app.register_blueprint(qualification_bp)
APP_VERSION = "1.30.0"
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
    "database_url": os.environ.get("MESFLOW_QA_DATABASE_URL", ""),
    "demo_target_url": os.environ.get("MESFLOW_QA_DEMO_TARGET_URL", QA_INTERNAL_URL),
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
    # The main QA execution context is always built into the machine running
    # QA Center. Persisted config from older releases must never redirect a
    # LOCAL/PRODUCTION_TEST machine to another Agent or host.
    base["base_url"] = QA_INTERNAL_URL
    base["internal_base_url"] = QA_INTERNAL_URL
    return base


def save_config(data: dict[str, Any]) -> None:
    payload=dict(data)
    # A legacy/custom base_url may remain in an old config file, but every
    # new save normalizes the main QA target back to this machine.
    payload["base_url"] = QA_INTERNAL_URL
    payload["internal_base_url"] = QA_INTERNAL_URL
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


def resolve_demo_target(cfg: dict[str, Any], requested: str | None = None) -> str:
    # Demo target is intentionally independent from QA_INTERNAL_ONLY. The main QA
    # dashboard may remain locked to Docker-internal MESFlow while the dedicated
    # Demo Center can point at a local host, another Docker stack, or a custom
    # test instance.
    raw = str(requested or cfg.get("demo_target_url") or QA_INTERNAL_URL).strip().rstrip("/")
    if not raw:
        raise ValueError("Chưa nhập Target MESFlow")
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Target phải là URL http:// hoặc https:// hợp lệ")
    if parsed.username or parsed.password:
        raise ValueError("Không đặt username/password trực tiếp trong Target URL")
    host = parsed.hostname.lower()
    if host in {"169.254.169.254", "metadata.google.internal"}:
        raise ValueError("Target metadata service không được phép")
    return raw


def demo_auth_preflight(cfg: dict[str, Any], requested_target: str | None = None) -> dict[str, Any]:
    base = resolve_demo_target(cfg, requested_target)
    username = str(cfg.get("username") or "").strip()
    password = str(cfg.get("password") or "")
    if not username or not password:
        raise ValueError("Chưa cấu hình tài khoản/mật khẩu MESFlow cho Demo Runner")
    session_client=requests.Session(); session_client.verify=False
    try:
        login=session_client.post(base+"/api/auth/login",json={"username":username,"password":password},timeout=20)
    except Exception as exc:
        raise RuntimeError(f"Không kết nối được MESFlow tại {base}: {exc}") from exc
    try: body=login.json() if login.content else {}
    except Exception: body={}
    if login.status_code==401:
        raise PermissionError("Sai tài khoản hoặc mật khẩu MESFlow. Hãy nhập lại credential trên màn hình Demo Center rồi bấm Kiểm tra đăng nhập.")
    if login.status_code>=400:
        raise RuntimeError(f"MESFlow login HTTP {login.status_code}: {body or login.text[:200]}")
    me=session_client.get(base+"/api/auth/me",timeout=20)
    try: me_body=me.json() if me.content else {}
    except Exception: me_body={}
    if me.status_code>=400:
        raise RuntimeError(f"Login trả thành công nhưng session không hợp lệ: HTTP {me.status_code} {me_body or me.text[:200]}")
    user=dict(me_body.get("user") or {})
    role=str(user.get("role") or "")
    permissions=list(user.get("permissions") or [])
    required={"employees.edit","template.edit","po.edit"}
    missing=[] if role.lower()=="admin" else sorted(required-set(permissions))
    return {"ok":True,"base_url":base,"username":username,"role":role,"display_name":user.get("display_name") or username,"missing_permissions":missing,"mesflow_user":user}


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



# Database reset safety model (v1.22.11)
#
# IMPORTANT: the old "full cleanup" implementation used TRUNCATE ... CASCADE
# across every non-preserved table. That is intentionally removed. QA reset is
# now performed only on a dedicated disposable database by cloning an immutable
# Golden Template database.
DB_RESET_PROTECTED_NAMES = {"mesflow", "postgres", "template0", "template1"}
DB_RESET_DEFAULT_ALLOWED_NAMES = {"mesflow_qa", "mesflow_test", "mesflow_demo"}


def _db_reset_enabled() -> bool:
    raw = os.environ.get("MESFLOW_QA_DB_ALLOW_RESET", os.environ.get("MESFLOW_QA_DB_ALLOW_CLEANUP", "0"))
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _cleanup_database_url(cfg: dict[str, Any]) -> str:
    # Kept as a compatibility helper name; this URL MUST now point at a
    # disposable QA database such as mesflow_qa, never the primary mesflow DB.
    return str(os.environ.get("MESFLOW_QA_DATABASE_URL") or cfg.get("database_url") or "").strip()


def _reset_template_database() -> str:
    return str(os.environ.get("MESFLOW_QA_TEMPLATE_DATABASE", "mesflow_qa_template")).strip()


def _allowed_reset_names() -> set[str]:
    raw = os.environ.get("MESFLOW_QA_DB_ALLOWED_NAMES", "mesflow_qa,mesflow_test,mesflow_demo")
    names = {x.strip() for x in raw.split(",") if x.strip()}
    return names or set(DB_RESET_DEFAULT_ALLOWED_NAMES)


def _safe_db_host(database_url: str) -> tuple[bool, str]:
    if not database_url:
        return False, "Chưa cấu hình MESFLOW_QA_DATABASE_URL"
    try:
        u = urlparse(database_url)
    except Exception:
        return False, "DATABASE_URL không hợp lệ"
    if u.scheme not in {"postgres", "postgresql"}:
        return False, "Reset chỉ hỗ trợ PostgreSQL"
    host = (u.hostname or "").lower()
    allowed = {x.strip().lower() for x in os.environ.get(
        "MESFLOW_QA_DB_ALLOWED_HOSTS", "mesflow-postgres,postgres,127.0.0.1,localhost"
    ).split(",") if x.strip()}
    if host not in allowed:
        return False, f"DB host {host!r} không nằm trong allowlist local: {sorted(allowed)}"
    return True, "OK"


def _safe_reset_target(database_url: str, template_database: str) -> tuple[bool, str, str]:
    ok, reason = _safe_db_host(database_url)
    if not ok:
        return False, reason, ""
    u = urlparse(database_url)
    db = unquote(u.path.lstrip("/"))
    if not db:
        return False, "DATABASE_URL không có tên database", ""
    if db in DB_RESET_PROTECTED_NAMES:
        return False, f"REFUSE_UNSAFE_DATABASE: {db} là database được bảo vệ", db
    allowed = _allowed_reset_names()
    if db not in allowed:
        return False, f"REFUSE_UNSAFE_DATABASE: {db!r} không nằm trong allowlist {sorted(allowed)}", db
    if not template_database:
        return False, "Chưa cấu hình MESFLOW_QA_TEMPLATE_DATABASE", db
    if template_database == db:
        return False, "Template database không được trùng target database", db
    if template_database in DB_RESET_PROTECTED_NAMES:
        return False, f"Template database {template_database!r} không hợp lệ", db
    if not template_database.endswith("_template"):
        return False, "Golden Template phải có hậu tố _template", db
    return True, "OK", db


def _database_url_for_name(database_url: str, db_name: str) -> str:
    u = urlparse(database_url)
    return u._replace(path=f"/{db_name}", query="", fragment="").geturl()


def _admin_database_url(database_url: str) -> str:
    configured = str(os.environ.get("MESFLOW_QA_DB_ADMIN_URL") or "").strip()
    return configured or _database_url_for_name(database_url, "postgres")


def _table_counts(database_url: str, names: list[str]) -> tuple[set[str], dict[str, int | None]]:
    with psycopg.connect(database_url, connect_timeout=8) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname='public'")
            present = {r[0] for r in cur.fetchall()}
            counts: dict[str, int | None] = {}
            for name in names:
                if name not in present:
                    counts[name] = None
                    continue
                cur.execute(sql.SQL("SELECT COUNT(*) FROM {}").format(sql.Identifier(name)))
                counts[name] = int(cur.fetchone()[0])
            return present, counts


def verify_qa_baseline(database_url: str, require_runtime_empty: bool = True) -> dict[str, Any]:
    required = {
        "employees": int(os.environ.get("MESFLOW_QA_DB_MIN_EMPLOYEES", "26")),
        "work_shifts": int(os.environ.get("MESFLOW_QA_DB_MIN_WORK_SHIFTS", "2")),
        "work_shift_intervals": int(os.environ.get("MESFLOW_QA_DB_MIN_SHIFT_INTERVALS", "4")),
        "templates": int(os.environ.get("MESFLOW_QA_DB_MIN_TEMPLATES", "1")),
        "users": int(os.environ.get("MESFLOW_QA_DB_MIN_USERS", "1")),
    }
    runtime_zero = ["production_orders", "parts", "operations", "work_sessions"]
    names = list(required) + runtime_zero
    try:
        present, counts = _table_counts(database_url, names)
    except Exception as exc:
        return {"ok": False, "error": f"VERIFY_CONNECT_FAILED: {type(exc).__name__}: {exc}", "checks": [], "counts": {}}
    checks = []
    ok = True
    for name, minimum in required.items():
        actual = counts.get(name)
        passed = actual is not None and actual >= minimum
        ok = ok and passed
        checks.append({"name": name, "rule": f">={minimum}", "actual": actual, "ok": passed})
    for name in runtime_zero:
        actual = counts.get(name)
        passed = actual is not None and (actual == 0 or not require_runtime_empty)
        ok = ok and passed
        checks.append({"name": name, "rule": "=0" if require_runtime_empty else "informational", "actual": actual, "ok": passed})
    return {"ok": ok, "checks": checks, "counts": counts, "tables_present": sorted(present),
            "require_runtime_empty": require_runtime_empty}


def qa_database_reset_preview(database_url: str) -> dict[str, Any]:
    if psycopg is None or sql is None:
        return {"ok": False, "enabled": _db_reset_enabled(), "error": "Thiếu psycopg; hãy dùng Docker image QA Center."}
    template = _reset_template_database()
    ok, reason, db = _safe_reset_target(database_url, template)
    if not _db_reset_enabled():
        return {"ok": False, "enabled": False, "error": "QA Database Reset đang khóa. Đặt MESFLOW_QA_DB_ALLOW_RESET=1 để bật."}
    if not ok:
        return {"ok": False, "enabled": True, "error": reason, "database": db, "template_database": template}
    admin_url = _admin_database_url(database_url)
    try:
        with psycopg.connect(admin_url, connect_timeout=8, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT current_user, COALESCE(inet_server_addr()::text,'local')")
                user, addr = cur.fetchone()
                cur.execute("SELECT datname FROM pg_database WHERE datname = ANY(%s)", ([db, template],))
                existing = {r[0] for r in cur.fetchall()}
    except Exception as exc:
        return {"ok": False, "enabled": True, "error": f"ADMIN_CONNECT_FAILED: {type(exc).__name__}: {exc}"}
    if template not in existing:
        return {"ok": False, "enabled": True, "error": f"GOLDEN_TEMPLATE_NOT_FOUND: {template}", "database": db, "template_database": template}
    template_url = _database_url_for_name(database_url, template)
    verification = verify_qa_baseline(template_url)
    if not verification.get("ok"):
        return {
            "ok": False, "enabled": True,
            "error": "GOLDEN_TEMPLATE_INVALID: baseline verification failed",
            "database": db, "template_database": template,
            "verification": verification,
        }
    return {
        "ok": True, "enabled": True, "host": urlparse(database_url).hostname,
        "database": db, "template_database": template, "target_exists": db in existing,
        "db_user": user, "server_addr": addr, "verification": verification,
        "confirm_text": f"RESET {db.upper()}",
        "mode": "DROP_CREATE_TEMPLATE", "destructive_scope": "QA_DATABASE_ONLY",
    }


def qa_database_reset_execute(database_url: str, confirm: str) -> dict[str, Any]:
    preview = qa_database_reset_preview(database_url)
    if not preview.get("ok"):
        raise RuntimeError(preview.get("error") or "QA Database Reset bị chặn")
    if str(confirm or "").strip() != preview["confirm_text"]:
        raise ValueError(f"Mã xác nhận phải chính xác: {preview['confirm_text']}")
    if active_runs():
        raise RuntimeError("Không thể reset database khi QA/Demo đang chạy. Hãy Stop các run trước.")
    db = str(preview["database"])
    template = str(preview["template_database"])
    admin_url = _admin_database_url(database_url)
    with psycopg.connect(admin_url, connect_timeout=8, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s AND pid<>pg_backend_pid()", (db,))
            cur.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(db)))
            cur.execute(sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(sql.Identifier(db), sql.Identifier(template)))
    target_url = _database_url_for_name(database_url, db)
    verification = verify_qa_baseline(target_url)
    if not verification.get("ok"):
        # Fail closed: never leave an invalid QA target available for testing.
        with psycopg.connect(admin_url, connect_timeout=8, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s AND pid<>pg_backend_pid()", (db,))
                cur.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(db)))
        raise RuntimeError(f"RESET_VERIFICATION_FAILED: {verification}")
    return {
        "ok": True, "database": db, "template_database": template,
        "mode": "DROP_CREATE_TEMPLATE", "verification": verification,
        "message": "QA database đã được tạo lại từ Golden Template và verify PASS",
    }


# Demo database workflow (v1.22.11)
# Source database is READ-ONLY from QA Center's point of view. All destructive
# cleanup happens only after cloning into the disposable demo template.
def _demo_source_database() -> str:
    return str(os.environ.get("MESFLOW_QA_DEMO_SOURCE_DATABASE", "mesflow")).strip() or "mesflow"

def _demo_database() -> str:
    return str(os.environ.get("MESFLOW_QA_DEMO_DATABASE", "mesflow_demo")).strip() or "mesflow_demo"

def _demo_template_database() -> str:
    return str(os.environ.get("MESFLOW_QA_DEMO_TEMPLATE_DATABASE", "mesflow_demo_template")).strip() or "mesflow_demo_template"

def _demo_runtime_roots() -> list[str]:
    raw = os.environ.get(
        "MESFLOW_QA_DEMO_RUNTIME_TABLES",
        "production_orders,audit_logs,exception_records,kiosk_events,penalty_tickets",
    )
    return [x.strip() for x in raw.split(",") if x.strip()]

def _demo_connection_urls(database_url: str) -> tuple[str, str, str, str]:
    source = _demo_source_database()
    target = _demo_database()
    template = _demo_template_database()
    if source in {target, template} or target == template:
        raise RuntimeError("DEMO_DATABASE_NAMES_CONFLICT")
    if target in DB_RESET_PROTECTED_NAMES or template in DB_RESET_PROTECTED_NAMES:
        raise RuntimeError("REFUSE_UNSAFE_DEMO_DATABASE")
    if target not in _allowed_reset_names():
        raise RuntimeError(f"REFUSE_UNSAFE_DATABASE: {target!r} không nằm trong allowlist {sorted(_allowed_reset_names())}")
    if not template.endswith("_template"):
        raise RuntimeError("Demo template phải có hậu tố _template")
    return (
        _database_url_for_name(database_url, source),
        _database_url_for_name(database_url, target),
        _database_url_for_name(database_url, template),
        _admin_database_url(database_url),
    )

def demo_database_preview(database_url: str) -> dict[str, Any]:
    if psycopg is None or sql is None:
        return {"ok": False, "enabled": False, "error": "Thiếu psycopg; hãy dùng Docker image QA Center."}
    if not _db_reset_enabled():
        return {"ok": False, "enabled": False, "error": "Database automation đang khóa. Đặt MESFLOW_QA_DB_ALLOW_RESET=1 để bật."}
    ok, reason = _safe_db_host(database_url)
    if not ok:
        return {"ok": False, "enabled": True, "error": reason}
    try:
        source_url, target_url, template_url, admin_url = _demo_connection_urls(database_url)
        source, target, template = _demo_source_database(), _demo_database(), _demo_template_database()
        with psycopg.connect(admin_url, connect_timeout=8, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT datname FROM pg_database WHERE datname = ANY(%s)", ([source, target, template],))
                existing = {r[0] for r in cur.fetchall()}
        if source not in existing:
            return {"ok": False, "enabled": True, "error": f"DEMO_SOURCE_NOT_FOUND: {source}"}
        # The live/source MESFlow database is expected to contain production
        # data. Only master/config minima are gates here; runtime counts are
        # informational. Empty-runtime enforcement belongs exclusively to the
        # disposable template and demo databases after clone cleanup.
        source_check = verify_qa_baseline(source_url, require_runtime_empty=False)
        template_check = verify_qa_baseline(template_url) if template in existing else None
        target_check = verify_qa_baseline(target_url) if target in existing else None
        return {
            "ok": True, "enabled": True,
            "source_database": source, "database": target, "template_database": template,
            "source_exists": True, "target_exists": target in existing, "template_exists": template in existing,
            "source_verification": source_check, "template_verification": template_check, "target_verification": target_check,
            "prepare_confirm_text": f"PREPARE {target.upper()}",
            "reset_confirm_text": f"RESET {target.upper()}",
            "mode": "AUTO_DEMO_BASELINE",
            "safety": "SOURCE_READ_ONLY_DESTRUCTIVE_CLONE_ONLY",
        }
    except Exception as exc:
        return {"ok": False, "enabled": True, "error": f"{type(exc).__name__}: {exc}"}

def _truncate_disposable_demo_clone(database_url: str) -> dict[str, Any]:
    # This function must only ever receive the newly-created disposable demo
    # template URL. The protected/source database is never passed here.
    expected = _demo_template_database()
    actual = unquote(urlparse(database_url).path.lstrip("/"))
    if actual != expected or actual in DB_RESET_PROTECTED_NAMES:
        raise RuntimeError(f"REFUSE_TRUNCATE_NON_DISPOSABLE_DATABASE: {actual}")
    cleaned = []
    with psycopg.connect(database_url, connect_timeout=8, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname='public'")
            present = {r[0] for r in cur.fetchall()}
            for table in _demo_runtime_roots():
                if table in present:
                    cur.execute(sql.SQL("TRUNCATE TABLE {} RESTART IDENTITY CASCADE").format(sql.Identifier(table)))
                    cleaned.append(table)
            if "employees" in present:
                cur.execute(
                    "DELETE FROM employees WHERE employee_no LIKE 'CODEX-DEMO-%' OR employee_no LIKE 'QAV%-EMP-%' OR employee_no LIKE 'DEMO-%'"
                )
    return {"tables": cleaned}

def demo_database_prepare_execute(database_url: str, confirm: str) -> dict[str, Any]:
    preview = demo_database_preview(database_url)
    if not preview.get("ok"):
        raise RuntimeError(preview.get("error") or "Demo database automation bị chặn")
    if str(confirm or "").strip() != preview["prepare_confirm_text"]:
        raise ValueError(f"Mã xác nhận phải chính xác: {preview['prepare_confirm_text']}")
    if active_runs():
        raise RuntimeError("Không thể chuẩn bị Demo DB khi QA/Demo đang chạy. Hãy Stop các run trước.")
    source, target, template = _demo_source_database(), _demo_database(), _demo_template_database()
    source_url, target_url, template_url, admin_url = _demo_connection_urls(database_url)
    with psycopg.connect(admin_url, connect_timeout=8, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = ANY(%s) AND pid<>pg_backend_pid()", ([target, template],))
            cur.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(target)))
            cur.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(template)))
            cur.execute(sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(sql.Identifier(template), sql.Identifier(source)))
    cleanup = _truncate_disposable_demo_clone(template_url)
    verification = verify_qa_baseline(template_url)
    if not verification.get("ok"):
        with psycopg.connect(admin_url, connect_timeout=8, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s AND pid<>pg_backend_pid()", (template,))
                cur.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(template)))
        raise RuntimeError(f"DEMO_TEMPLATE_VERIFICATION_FAILED: {verification}")
    with psycopg.connect(admin_url, connect_timeout=8, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(sql.Identifier(target), sql.Identifier(template)))
    target_verification = verify_qa_baseline(target_url)
    if not target_verification.get("ok"):
        raise RuntimeError(f"DEMO_TARGET_VERIFICATION_FAILED: {target_verification}")
    return {
        "ok": True, "source_database": source, "database": target, "template_database": template,
        "cleanup": cleanup, "verification": target_verification,
        "message": "Demo baseline và Demo DB đã được tạo tự động; source database không bị chỉnh sửa.",
    }

def demo_database_reset_execute(database_url: str, confirm: str) -> dict[str, Any]:
    preview = demo_database_preview(database_url)
    if not preview.get("ok"):
        raise RuntimeError(preview.get("error") or "Demo database reset bị chặn")
    if not preview.get("template_exists"):
        raise RuntimeError("DEMO_TEMPLATE_NOT_FOUND: hãy bấm Chuẩn bị Demo DB trước")
    if str(confirm or "").strip() != preview["reset_confirm_text"]:
        raise ValueError(f"Mã xác nhận phải chính xác: {preview['reset_confirm_text']}")
    if active_runs():
        raise RuntimeError("Không thể reset Demo DB khi QA/Demo đang chạy. Hãy Stop các run trước.")
    target, template = _demo_database(), _demo_template_database()
    _, target_url, template_url, admin_url = _demo_connection_urls(database_url)
    template_verification = verify_qa_baseline(template_url)
    if not template_verification.get("ok"):
        raise RuntimeError(f"DEMO_TEMPLATE_INVALID: {template_verification}")
    with psycopg.connect(admin_url, connect_timeout=8, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s AND pid<>pg_backend_pid()", (target,))
            cur.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(target)))
            cur.execute(sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(sql.Identifier(target), sql.Identifier(template)))
    verification = verify_qa_baseline(target_url)
    if not verification.get("ok"):
        raise RuntimeError(f"DEMO_RESET_VERIFICATION_FAILED: {verification}")
    return {"ok": True, "database": target, "template_database": template, "verification": verification, "message": "Demo DB đã trở về baseline sạch."}


# Legacy destructive full-cleanup API is permanently disabled. Keeping these
# names as explicit refusal helpers prevents old clients from reaching any
# TRUNCATE implementation after an upgrade.
def postgres_cleanup_preview(database_url: str) -> dict[str, Any]:
    return {"ok": False, "enabled": False, "error": "FULL_CLEANUP_DISABLED: dùng Reset QA Database từ Golden Template"}


def postgres_cleanup_execute(database_url: str, confirm: str) -> dict[str, Any]:
    raise RuntimeError("FULL_CLEANUP_DISABLED: dùng Reset QA Database từ Golden Template")


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


# --- QA Center Release Package Builder (Build Once / Promote Same Artifact) ---
# NOT the same thing as RELEASE_PROFILES below: release-local/release-test
# are QA *test* gates run against an already-deployed target. This section
# is the actual package builder -- it shells out to the SAME
# scripts/build-release.sh Deploy Agent's own DEV-only QA build trigger
# uses (deploy-agent/agent.py, "QA build (DEV only)" section); no second
# build implementation, no duplicated version/contamination-guard logic.
# QA Center never deploys anything itself -- uploading the resulting ZIP to
# Deploy Agent (POST /qa-release/upload) and deploying it
# (/qa-release/deploy/<version>) remains the only path to a running
# environment. Only ever usable when this process is running from an
# actual qa-center git checkout next to scripts/build-release.sh (local/DEV
# usage): the deployed Docker image only ever contains current/'s contents
# (docker/Dockerfile: `COPY . /app`, WORKDIR /app), so QA_BUILD_SCRIPT does
# not exist there and every endpoint below reports BUILD_NOT_AVAILABLE_HERE
# automatically -- this never runs inside a shipped QA Center container.
QA_REPO_ROOT = ROOT.parent
QA_BUILD_SCRIPT = QA_REPO_ROOT / "scripts" / "build-release.sh"
QA_RELEASES_ROOT = QA_REPO_ROOT.parent / "artifacts" / "qa-center" / "releases"
QA_BUILD_JOB_FILE = STATE_DIR / "release_build_job.json"
_qa_build_lock = threading.Lock()
_qa_build_job_file_lock = threading.Lock()


def _qa_build_available() -> bool:
    return QA_BUILD_SCRIPT.is_file() and shutil.which("docker") is not None


def _qa_git(*args: str, timeout: int = 5) -> str:
    try:
        r = subprocess.run(["git", "-C", str(QA_REPO_ROOT), "-c", "safe.directory=*", *args],
                            capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def _qa_working_tree_status() -> str:
    return "DIRTY" if _qa_git("status", "--porcelain") else "CLEAN"


def _qa_source_version() -> str:
    f = ROOT / "VERSION"
    return f.read_text(encoding="utf-8-sig").strip() if f.is_file() else ""


def _qa_canon(v: str) -> str:
    return str(v or "").strip().lstrip("Vv").strip()


def _qa_latest_frozen_release() -> dict[str, Any] | None:
    if not QA_RELEASES_ROOT.is_dir():
        return None
    best: tuple[float, dict[str, Any]] | None = None
    for d in QA_RELEASES_ROOT.iterdir():
        rj = d / "release.json"
        if not rj.is_file():
            continue
        try:
            meta = json.loads(rj.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        mtime = rj.stat().st_mtime
        if best is None or mtime > best[0]:
            best = (mtime, meta)
    return best[1] if best else None


def _qa_package_info(version: str) -> dict[str, Any] | None:
    dist = QA_RELEASES_ROOT / version
    package = dist / f"QACenter_{version}.deploy.zip"
    if not package.is_file():
        return None
    release_meta: dict[str, Any] = {}
    if (dist / "release.json").is_file():
        try:
            release_meta = json.loads((dist / "release.json").read_text(encoding="utf-8-sig"))
        except Exception:
            release_meta = {}
    manifest: dict[str, Any] = {}
    if (dist / "manifest.json").is_file():
        try:
            manifest = json.loads((dist / "manifest.json").read_text(encoding="utf-8-sig"))
        except Exception:
            manifest = {}
    sha_file = dist / f"QACenter_{version}.deploy.zip.sha256"
    sha256 = sha_file.read_text(encoding="utf-8").split()[0] if sha_file.is_file() else ""
    return {
        "version": version,
        "filename": package.name,
        "size_bytes": package.stat().st_size,
        "sha256": sha256,
        "source_commit": release_meta.get("source_commit", ""),
        "built_at": release_meta.get("built_at", ""),
        "manifest": manifest,
    }


def _qa_build_job_read() -> dict[str, Any]:
    with _qa_build_job_file_lock:
        if QA_BUILD_JOB_FILE.is_file():
            try:
                return json.loads(QA_BUILD_JOB_FILE.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}


def _qa_build_job_update(status: str, message: str, **extra: Any) -> dict[str, Any]:
    with _qa_build_job_file_lock:
        job: dict[str, Any] = {}
        if QA_BUILD_JOB_FILE.is_file():
            try:
                job = json.loads(QA_BUILD_JOB_FILE.read_text(encoding="utf-8"))
            except Exception:
                job = {}
        job.update(status=status, message=message, updated_at=now(), **extra)
        QA_BUILD_JOB_FILE.parent.mkdir(parents=True, exist_ok=True)
        QA_BUILD_JOB_FILE.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
        return job


def _run_qa_release_build() -> None:
    """Runs in a background thread (started by _start_qa_release_build).
    The UI polls /api/release/build-status (lightweight JSON, no logs)
    while status is QUEUED/BUILDING and stops once it sees a terminal
    SUCCESS/FAILED -- full logs are fetched separately, on demand, via
    /api/release/build-log."""
    log_path = LOG_DIR / "release-build.log"
    try:
        version_at_start = _qa_source_version()
        _qa_build_job_update("BUILDING", "Đang build QA Center release ZIP…",
                              version=version_at_start, started_at=now(), log_file=str(log_path))
        with log_path.open("w", encoding="utf-8") as log:
            result = subprocess.run([str(QA_BUILD_SCRIPT)], cwd=str(QA_REPO_ROOT),
                                     stdout=log, stderr=subprocess.STDOUT, text=True, timeout=1800)
        full_log = log_path.read_text(encoding="utf-8", errors="replace")
        tail_lines = full_log.splitlines()[-120:]
        if result.returncode != 0:
            code = "VERSION_ALREADY_RELEASED" if "VERSION_ALREADY_RELEASED" in full_log else "BUILD_FAILED"
            _qa_build_job_update("FAILED", code, error_code=code, log_tail=tail_lines)
            return
        version = _qa_source_version()
        package = _qa_package_info(version)
        if not package:
            _qa_build_job_update("FAILED", "PACKAGE_NOT_FOUND", error_code="PACKAGE_NOT_FOUND", log_tail=tail_lines)
            return
        _qa_build_job_update("SUCCESS", "Build hoàn tất", version=version, package=package, log_tail=tail_lines)
    except subprocess.TimeoutExpired:
        _qa_build_job_update("FAILED", "BUILD_TIMEOUT", error_code="BUILD_TIMEOUT")
    except Exception as exc:
        _qa_build_job_update("FAILED", f"{type(exc).__name__}: {exc}", error_code="BUILD_FAILED")
    finally:
        if _qa_build_lock.locked():
            _qa_build_lock.release()


def _start_qa_release_build() -> dict[str, Any]:
    if not _qa_build_available():
        raise RuntimeError("BUILD_NOT_AVAILABLE_HERE")
    if not _qa_build_lock.acquire(False):
        raise RuntimeError("BUILD_ALREADY_RUNNING")
    try:
        job = _qa_build_job_update("QUEUED", "Đã xếp hàng build")
        threading.Thread(target=_run_qa_release_build, daemon=True).start()
        return job
    except Exception:
        _qa_build_lock.release()
        raise


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
    base = str(cfg.get("base_url") or QA_INTERNAL_URL).rstrip("/")
    emit(state, f"Kiểm thử MESFlow target: {base}")
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


DEMO_SCENARIOS = {
    "full-production": {"name":"Full Production Demo","description":"Template → PO → Kiosk → Session → Material Flow → Dashboard → Trace/Audit"},
    "planning-po": {"name":"Planning & Production Order","description":"Template, Production Order và Gantt/Material Flow"},
    "kiosk-realtime": {"name":"Kiosk & Realtime","description":"Quét thẻ, Operation, start/finish, sản lượng và realtime dashboard"},
    "quality-rework": {"name":"Quality / Defect / Rework","description":"Sản lượng đạt, lỗi, rework và ảnh hưởng tiến độ"},
    "trace-audit": {"name":"Traceability & Audit","description":"Production Trace, Business Audit, Session và System Logs"},
    "feature-tour": {"name":"MESFlow Feature Tour","description":"Đi qua toàn bộ màn hình chính để giới thiệu tính năng"},
}

def demo_worker(state: RunState, cfg: dict[str, Any], options: dict[str, Any]) -> None:
    scenario=str(options.get("scenario") or "full-production")
    if scenario not in DEMO_SCENARIOS:
        finish(state,"FAILED",f"Demo scenario không hợp lệ: {scenario}"); return
    if not str(cfg.get("password") or "").strip():
        finish(state,"FAILED","Chưa cấu hình mật khẩu MESFlow cho Demo Runner"); return
    out=REPORT_DIR / state.run_id / "demo"; out.mkdir(parents=True,exist_ok=True)
    def finalize_ownership(status: str) -> None:
        path=out/"ownership.json"
        if not path.is_file(): return
        try:
            owned=json.loads(path.read_text(encoding="utf-8")); owned["status"]=status
            owned["finished_at"]=now(); path.write_text(json.dumps(owned,ensure_ascii=False,indent=2),encoding="utf-8")
        except (OSError,json.JSONDecodeError): pass
    base=resolve_demo_target(cfg, str(options.get("target_url") or ""))
    pace=max(0.25,min(5.0,float(options.get("pace",1.0) or 1.0)))
    mode=str(options.get("mode") or "auto").lower()
    if mode not in {"auto","presenter","manual"}: mode="auto"
    cmd=[sys.executable,"-u",str(ROOT/"demo_runner.py"),"--run-id",state.run_id,"--scenario",scenario,"--base-url",base,"--username",str(cfg.get("username") or "admin"),"--password",str(cfg.get("password") or ""),"--output-dir",str(out),"--pace",str(pace),"--mode",mode]
    emit(state,f"Demo Runner: {DEMO_SCENARIOS[scenario]['name']} · pace={pace}x · mode={mode}")
    try:
        env=os.environ.copy(); env.update({"PYTHONUNBUFFERED":"1","PYTHONUTF8":"1","PYTHONIOENCODING":"utf-8"})
        proc=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding="utf-8",errors="replace",env=env)
        state.process=proc; state.pid=proc.pid
        assert proc.stdout is not None
        for line in proc.stdout:
            value=line.rstrip(); emit(state,value)
            if value.startswith("DEMO_EVENT|"):
                try:
                    ev=json.loads(value.split("|",1)[1]); kind=ev.get("kind")
                    if kind=="step_start": state.message=f"Demo: {ev.get('title','')}"
                    elif kind=="step_pass": state.total+=1; state.passed+=1
                    elif kind in {"step_fail","fatal"}: state.failed+=1
                except Exception: pass
            if state.stop_event.is_set(): proc.terminate(); break
        code=proc.wait(timeout=20)
        if state.stop_event.is_set(): finalize_ownership("STOPPED"); finish(state,"STOPPED","Đã dừng Demo Runner")
        elif code==0: finalize_ownership("PASSED"); finish(state,"PASSED",f"Demo hoàn tất: {DEMO_SCENARIOS[scenario]['name']}")
        else:
            detail=""
            try:
                state_file=out/"state.json"
                if state_file.exists():
                    demo_state=json.loads(state_file.read_text(encoding="utf-8"))
                    detail=str(demo_state.get("error") or demo_state.get("current_action",{}).get("error") or "")
            except Exception:
                detail=""
            finalize_ownership("FAILED"); finish(state,"FAILED",f"Demo hoàn tất với lỗi (exit {code})" + (f": {detail}" if detail else ""))
    except Exception as exc:
        finalize_ownership("FAILED"); finish(state,"FAILED",f"Không chạy được Demo Runner: {exc}")


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


@app.get("/demo")
def demo_page():
    cfg=load_config()
    target=resolve_demo_target(cfg)
    response=make_response(render_template("demo.html",config=cfg,app_version=APP_VERSION,target_url=target))
    response.headers["Cache-Control"]="no-store, no-cache, must-revalidate, max-age=0"
    return response


@app.post("/api/demo/preflight")
def api_demo_preflight():
    payload=request.get_json(silent=True) or {}
    cfg=load_config()
    if "username" in payload: cfg["username"]=str(payload.get("username") or "").strip()
    if "password" in payload: cfg["password"]=str(payload.get("password") or "")
    target=str(payload.get("target_url") or cfg.get("demo_target_url") or QA_INTERNAL_URL).strip()
    try:
        target=resolve_demo_target(cfg,target)
        cfg["demo_target_url"]=target
        if bool(payload.get("save",True)):
            save_config(cfg)
        result=demo_auth_preflight(cfg,target)
        return jsonify(result),200
    except PermissionError as exc:
        return jsonify({"ok":False,"error":"INVALID_CREDENTIALS","message":str(exc),"target":target}),401
    except ValueError as exc:
        return jsonify({"ok":False,"error":"TARGET_OR_CREDENTIALS_INVALID","message":str(exc),"target":target}),400
    except Exception as exc:
        return jsonify({"ok":False,"error":"AUTH_PREFLIGHT_FAILED","message":str(exc),"target":target}),502


@app.get("/api/version")
def api_version():
    cfg=load_config()
    return jsonify({"ok": True, "app": "MESFlow QA Center", "version": APP_VERSION, "profile": QA_PROFILE, "mode": "built-in-local", "target": QA_INTERNAL_URL})


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
    cfg=load_config()
    return jsonify({"ok": True, "runs": runs, "system": system, "version": APP_VERSION, "mode": "built-in-local", "target": QA_INTERNAL_URL})


@app.get("/api/release/info")
def api_release_info():
    version = _qa_source_version()
    latest = _qa_latest_frozen_release()
    already_released = bool(latest) and _qa_canon(str(latest.get("version"))) == _qa_canon(version)
    return jsonify({
        "ok": True,
        "build_available": _qa_build_available(),
        "source": {
            "version": version,
            "git_commit": _qa_git("rev-parse", "--short", "HEAD") or "unknown",
            "working_tree": _qa_working_tree_status(),
        },
        "latest_release": latest,
        "current_version_already_released": already_released,
        "current_version_package": _qa_package_info(version) if already_released and version else None,
        "build_job": _qa_build_job_read(),
    })


@app.post("/api/release/build")
def api_release_build():
    try:
        job = _start_qa_release_build()
        return jsonify({"ok": True, "job": job}), 202
    except RuntimeError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 409


@app.get("/api/release/build-status")
def api_release_build_status():
    # Lightweight poll target: no logs, just status/message/package. UI
    # polls this only while BUILDING and stops on SUCCESS/FAILED.
    return jsonify({"ok": True, "job": _qa_build_job_read()})


@app.get("/api/release/build-log")
def api_release_build_log():
    # On-demand only -- never auto-polled continuously.
    job = _qa_build_job_read()
    log_file = job.get("log_file")
    if not log_file or not Path(log_file).is_file():
        return jsonify({"ok": False, "error": "NO_LOG"}), 404
    text = Path(log_file).read_text(encoding="utf-8", errors="replace")
    return jsonify({"ok": True, "log": text[-40000:]})


@app.get("/api/release/download/<version>")
def api_release_download(version: str):
    # Must look like an actual build-release.sh version (X.Y.Z or X.Y.Z.W)
    # -- not merely "path-safe" characters. A looser charset that allows
    # dots (e.g. "[0-9A-Za-z._-]+") would accept ".." as a "valid" version
    # and let QA_RELEASES_ROOT / version escape one directory level even
    # though Flask's route converter already blocks embedded "/".
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(\.[0-9]+)?", version):
        return jsonify({"ok": False, "error": "INVALID_VERSION"}), 400
    package = QA_RELEASES_ROOT / version / f"QACenter_{version}.deploy.zip"
    if not package.is_file():
        return jsonify({"ok": False, "error": "PACKAGE_NOT_FOUND"}), 404
    return send_file(str(package), as_attachment=True, download_name=package.name, mimetype="application/zip")


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
    allowed = {"functional", "api_soak", "browser_visual", "behavioral", "factory_simulation", "realtime_soak", "demo"}
    if test_type not in allowed:
        return jsonify({"error": "Loại test không hợp lệ"}), 400
    cfg = load_config()
    if test_type == "demo":
        requested_target=str(payload.get("target_url") or cfg.get("demo_target_url") or QA_INTERNAL_URL).strip()
        try:
            requested_target=resolve_demo_target(cfg,requested_target)
            cfg["demo_target_url"]=requested_target
            save_config(cfg)
            demo_auth_preflight(cfg,requested_target)
        except PermissionError as exc:
            return jsonify({"ok":False,"error":"INVALID_CREDENTIALS","message":str(exc)}),401
        except ValueError as exc:
            return jsonify({"ok":False,"error":"CREDENTIALS_REQUIRED","message":str(exc)}),400
        except Exception as exc:
            return jsonify({"ok":False,"error":"DEMO_PREFLIGHT_FAILED","message":str(exc)}),502
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
        "demo": demo_worker,
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


@app.get("/api/demo/scenarios")
def api_demo_scenarios():
    return jsonify({"ok":True,"items":[{"id":k,**v} for k,v in DEMO_SCENARIOS.items()]})


def _demo_ownership_path(run_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{5,79}", str(run_id or "")):
        raise ValueError("RUN_ID_INVALID")
    return REPORT_DIR / run_id / "demo" / "ownership.json"


def _read_demo_ownership(run_id: str) -> dict[str, Any]:
    path=_demo_ownership_path(run_id)
    if not path.is_file(): raise FileNotFoundError("DEMO_OWNERSHIP_NOT_FOUND")
    data=json.loads(path.read_text(encoding="utf-8"))
    if data.get("run_id") != run_id: raise ValueError("DEMO_OWNERSHIP_MISMATCH")
    return data


@app.get("/api/demo/runs")
def api_demo_runs():
    items=[]
    for path in REPORT_DIR.glob("*/demo/ownership.json"):
        try:
            item=json.loads(path.read_text(encoding="utf-8"))
            if item.get("run_id")==path.parent.parent.name: items.append(item)
        except (OSError,json.JSONDecodeError): pass
    items.sort(key=lambda x:str(x.get("created_at") or ""),reverse=True)
    return jsonify(ok=True,items=items)


@app.get("/api/demo/<run_id>/generated-data")
def api_demo_generated_data(run_id: str):
    try: return jsonify(ok=True,run=_read_demo_ownership(run_id))
    except FileNotFoundError as exc: return jsonify(ok=False,error=str(exc)),404
    except (ValueError,OSError,json.JSONDecodeError) as exc: return jsonify(ok=False,error=str(exc)),400


@app.post("/api/demo/<run_id>/cleanup")
def api_demo_cleanup(run_id: str):
    try:
        ownership=_read_demo_ownership(run_id)
        if ownership.get("cleanup_status")=="CLEANED":
            return jsonify(ok=True,already_cleaned=True,result=ownership.get("cleanup_result") or {})
        state=_runs.get(run_id)
        if state and state.status=="RUNNING": return jsonify(ok=False,error="DEMO_RUN_ACTIVE"),409
        payload=request.get_json(silent=True) or {}
        if payload.get("confirm_run_id") != run_id: return jsonify(ok=False,error="CONFIRM_RUN_ID_MISMATCH"),400
        generated=ownership.get("generated") or {}; marker=f"QA_RUN_ID={run_id}"
        cfg=load_config(); base=str(ownership.get("target_url") or "").rstrip("/")
        sess=requests.Session(); sess.verify=False
        login=sess.post(base+"/api/auth/login",json={"username":cfg.get("username"),"password":cfg.get("password")},timeout=20)
        if login.status_code>=400: raise RuntimeError(f"MESFLOW_LOGIN_FAILED HTTP {login.status_code}")
        def get_item(resource,item):
            if not item or not item.get("id"): return None
            r=sess.get(f"{base}/api/{resource}/{item['id']}",timeout=20)
            if r.status_code==404:return None
            r.raise_for_status(); return (r.json().get("item") or {})
        po_meta=generated.get("production_order"); tpl_meta=generated.get("template"); emp_meta=generated.get("employee")
        po=get_item("production-orders",po_meta); tpl=get_item("templates",tpl_meta); emp=get_item("employees",emp_meta)
        if po and (po.get("code")!=po_meta.get("code") or str(po.get("notes") or "").strip()!=marker): raise RuntimeError("PO_OWNERSHIP_MISMATCH")
        if tpl and (tpl.get("code")!=tpl_meta.get("code") or str(tpl.get("product") or "").strip()!=marker): raise RuntimeError("TEMPLATE_OWNERSHIP_MISMATCH")
        if emp and (emp.get("employee_no")!=emp_meta.get("code") or str(emp.get("department") or "").strip()!=marker): raise RuntimeError("EMPLOYEE_OWNERSHIP_MISMATCH")
        deleted={}
        if po:
            r=sess.delete(f"{base}/api/production-orders/{po['id']}/force",json={"confirm_code":po["code"],"qa_run_id":run_id},timeout=30)
            if r.status_code>=400: raise RuntimeError(f"PO_CLEANUP_FAILED HTTP {r.status_code}: {r.text[:500]}")
            deleted["production_order"]=r.json()
        for resource,key,item in (("templates","template",tpl),("employees","employee",emp)):
            if item:
                r=sess.delete(f"{base}/api/{resource}/{item['id']}",timeout=20)
                if r.status_code>=400: raise RuntimeError(f"{key.upper()}_CLEANUP_FAILED HTTP {r.status_code}: {r.text[:500]}")
                deleted[key]=1
        remaining={"production_order":bool(get_item("production-orders",po_meta)),"template":bool(get_item("templates",tpl_meta)),"employee":bool(get_item("employees",emp_meta))}
        if any(remaining.values()): raise RuntimeError(f"CLEANUP_INCOMPLETE: {remaining}")
        result={"run_id":run_id,"cleaned_at":now(),"deleted":deleted,"remaining":remaining,"remaining_qa_records":0}
        ownership.update(cleanup_status="CLEANED",cleaned_at=result["cleaned_at"],cleanup_result=result)
        path=_demo_ownership_path(run_id); path.write_text(json.dumps(ownership,ensure_ascii=False,indent=2),encoding="utf-8")
        (path.parent/"cleanup.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
        return jsonify(ok=True,result=result)
    except FileNotFoundError as exc: return jsonify(ok=False,error=str(exc)),404
    except Exception as exc: return jsonify(ok=False,error="DEMO_CLEANUP_FAILED",message=str(exc)),409


def _redact_bug_text(value: Any) -> str:
    text = str(value if value is not None else "")
    # Bearer must be redacted before generic Authorization assignment handling.
    text = re.sub(r"(?i)Bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer ***", text)
    # URL credentials: scheme://user:password@host -> scheme://user:***@host
    text = re.sub(r"(?i)([a-z][a-z0-9+.-]*://[^:/\s]+:)[^@/\s]+(@)", r"\1***\2", text)
    # Common secret assignments / JSON-ish fields. Quoted JSON and unquoted env/log forms.
    text = re.sub(r'(?i)(["\']?(?:password|passwd|secret|token|authorization|api[_-]?key)["\']?\s*[:=]\s*)["\']?[^\s,;\"\'}]+["\']?', r"\1***", text)
    return text


def _safe_json(value: Any) -> Any:
    try:
        return json.loads(_redact_bug_text(json.dumps(value, ensure_ascii=False, default=str)))
    except Exception:
        return _redact_bug_text(value)


def _demo_system_snapshot() -> dict[str, Any]:
    snap: dict[str, Any] = {
        "qa_center_version": APP_VERSION,
        "qa_profile": QA_PROFILE,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "hostname": platform.node(),
        "pid": os.getpid(),
    }
    try:
        snap["loadavg"] = list(os.getloadavg())
    except Exception:
        pass
    if psutil is not None:
        try:
            vm=psutil.virtual_memory(); snap["memory"]={"total":vm.total,"available":vm.available,"percent":vm.percent}
            snap["process"]={"rss":psutil.Process().memory_info().rss,"cpu_percent":psutil.Process().cpu_percent(interval=0.0)}
        except Exception:
            pass
    return snap


def _demo_target_snapshot(cfg: dict[str, Any], target: str) -> dict[str, Any]:
    result: dict[str, Any] = {"target": _redact_bug_text(target)}
    username=str(cfg.get("username") or "").strip(); password=str(cfg.get("password") or "")
    if not target or not username or not password:
        result["probe"]="skipped: missing target or credentials"
        return result
    sess=requests.Session(); sess.verify=False
    try:
        login=sess.post(target.rstrip("/")+"/api/auth/login",json={"username":username,"password":password},timeout=8)
        result["login_http"]=login.status_code
        if login.status_code < 400:
            for name,path in (("version","/api/system/version"),("health","/api/system/health"),("me","/api/auth/me")):
                try:
                    r=sess.get(target.rstrip("/")+path,timeout=8)
                    try: body=r.json() if r.content else {}
                    except Exception: body=r.text[:500]
                    if name=="me" and isinstance(body,dict):
                        user=dict(body.get("user") or {})
                        body={"ok":body.get("ok",r.ok),"user":{"id":user.get("id"),"username":user.get("username"),"role":user.get("role"),"display_name":user.get("display_name")}}
                    result[name]={"http":r.status_code,"body":_safe_json(body)}
                except Exception as exc:
                    result[name]={"error":_redact_bug_text(exc)}
    except Exception as exc:
        result["probe_error"]=_redact_bug_text(exc)
    return result


def _demo_db_snapshot(cfg: dict[str, Any]) -> dict[str, Any]:
    url=str(cfg.get("database_url") or "").strip()
    if not url:
        return {"configured":False}
    result={"configured":True,"source":_demo_source_database(),"template":_demo_template_database(),"demo":_demo_database()}
    try:
        preview=demo_database_preview(url)
        # Keep only structural/count information; never expose database_url.
        result["preview"]=_safe_json({k:v for k,v in preview.items() if k not in {"database_url","admin_url","source_url","target_url","template_url"}})
    except Exception as exc:
        result["error"]=_redact_bug_text(exc)
    return result


def _build_demo_bug_report(run_id: str) -> tuple[str, dict[str, Any], Path, Path]:
    state=_runs.get(run_id)
    if not state or state.test_type!="demo":
        raise FileNotFoundError("DEMO_RUN_NOT_FOUND")
    folder=REPORT_DIR/run_id/"demo"; folder.mkdir(parents=True,exist_ok=True)
    demo_state: dict[str,Any]={}
    state_path=folder/"state.json"
    if state_path.exists():
        try: demo_state=json.loads(state_path.read_text(encoding="utf-8"))
        except Exception as exc: demo_state={"state_read_error":str(exc)}
    cfg=load_config(); target=str(demo_state.get("base_url") or cfg.get("demo_target_url") or cfg.get("base_url") or QA_INTERNAL_URL).rstrip("/")
    # demo_runner state before 1.22.16 does not persist base_url, so target above safely falls back to config.
    failure=demo_state.get("error_detail") or {}
    if not failure and demo_state.get("current_action",{}).get("error"):
        a=demo_state.get("current_action") or {}; c=demo_state.get("current_case") or {}
        failure={"stage":"testcase","case":c.get("title") or demo_state.get("current_title"),"action":a.get("kind"),"target":a.get("target"),"message":a.get("error"),"url":target}
    screenshots=[]
    shot_dir=folder/"screenshots"
    if shot_dir.exists(): screenshots=[x.name for x in sorted(shot_dir.glob("*.png"),key=lambda z:z.stat().st_mtime)]
    payload={
        "report_schema":"MESFLOW_QA_GPT_BUG_REPORT_V1",
        "generated_at":now(),
        "run":state.public(),
        "failure":failure or demo_state.get("error") or state.message,
        "scenario":{"id":demo_state.get("scenario"),"name":demo_state.get("scenario_name"),"mode":demo_state.get("mode"),"status":demo_state.get("status")},
        "current_case":demo_state.get("current_case"),
        "current_action":demo_state.get("current_action"),
        "plan":demo_state.get("plan") or [],
        "results":demo_state.get("results") or [],
        "recent_actions":(demo_state.get("action_log") or [])[-40:],
        "screenshots":screenshots[-20:],
        "system":_demo_system_snapshot(),
        "mesflow":_demo_target_snapshot(cfg,target),
        "database":_demo_db_snapshot(cfg),
        "recent_runner_logs":state.log_lines[-120:],
    }
    payload=_safe_json(payload)
    fail=payload.get("failure") or {}
    if isinstance(fail,dict):
        fail_summary=f"{fail.get('stage','')} | {fail.get('case','')} | {fail.get('action','')} | {fail.get('target','')} | {fail.get('message','')}"
    else: fail_summary=str(fail)
    lines=[
        "# MESFlow QA — GPT Bug Report",
        "",
        "> Paste this report directly into GPT/Codex. Secrets are redacted by QA Center.",
        "",
        "## Fix request",
        "Analyze the failure below, identify the most likely root cause, point to the failing testcase/action/selector/API, and propose the smallest safe code fix plus regression test. Do not assume missing evidence; call out anything still needed.",
        "",
        "## Failure summary",
        f"- Run: `{run_id}`",
        f"- QA Center: `{APP_VERSION}` / profile `{QA_PROFILE}`",
        f"- Scenario: `{payload.get('scenario',{}).get('name') or payload.get('scenario',{}).get('id') or 'unknown'}`",
        f"- Run status: `{payload.get('run',{}).get('status')}`",
        f"- Failure: `{_redact_bug_text(fail_summary)}`",
        "",
        "## Current testcase",
        "```json", json.dumps(payload.get("current_case"),ensure_ascii=False,indent=2), "```",
        "",
        "## Current action",
        "```json", json.dumps(payload.get("current_action"),ensure_ascii=False,indent=2), "```",
        "",
        "## Failure detail",
        "```json", json.dumps(payload.get("failure"),ensure_ascii=False,indent=2), "```",
        "",
        "## Test results",
        "```json", json.dumps(payload.get("results"),ensure_ascii=False,indent=2), "```",
        "",
        "## Recent browser actions",
        "```json", json.dumps(payload.get("recent_actions"),ensure_ascii=False,indent=2), "```",
        "",
        "## MESFlow target snapshot",
        "```json", json.dumps(payload.get("mesflow"),ensure_ascii=False,indent=2), "```",
        "",
        "## Database snapshot",
        "```json", json.dumps(payload.get("database"),ensure_ascii=False,indent=2), "```",
        "",
        "## QA/system snapshot",
        "```json", json.dumps(payload.get("system"),ensure_ascii=False,indent=2), "```",
        "",
        "## Evidence files",
        *(f"- `{x}`" for x in payload.get("screenshots",[]) or ["No screenshots"]),
        "",
        "## Recent runner logs",
        "```text", "\n".join(_redact_bug_text(x) for x in payload.get("recent_runner_logs",[])), "```",
    ]
    text="\n".join(lines)+"\n"
    md=folder/"GPT_BUG_REPORT.md"; js=folder/"GPT_BUG_REPORT.json"; bundle=folder/"GPT_BUG_BUNDLE.zip"
    md.write_text(text,encoding="utf-8"); js.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding="utf-8")
    with zipfile.ZipFile(bundle,"w",compression=zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        z.write(md,md.name); z.write(js,js.name)
        if state_path.exists(): z.write(state_path,"state.json")
        if state.log_file and Path(state.log_file).exists(): z.write(state.log_file,"qa-run.log")
        for name in screenshots[-10:]:
            f=shot_dir/name
            if f.exists(): z.write(f,f"screenshots/{name}")
    return text,payload,md,bundle


@app.get("/api/demo/<run_id>/bug-report")
def api_demo_bug_report(run_id: str):
    try:
        text,payload,md,bundle=_build_demo_bug_report(run_id)
        return jsonify({"ok":True,"text":text,"summary":{"failure":payload.get("failure"),"run":payload.get("run"),"system":payload.get("system")},"report_url":f"/api/demo/{run_id}/bug-report.md","bundle_url":f"/api/demo/{run_id}/bug-bundle.zip"})
    except FileNotFoundError:
        return jsonify({"ok":False,"error":"DEMO_RUN_NOT_FOUND"}),404
    except Exception as exc:
        return jsonify({"ok":False,"error":"BUG_REPORT_FAILED","message":_redact_bug_text(exc)}),500


@app.get("/api/demo/<run_id>/bug-report.md")
def api_demo_bug_report_download(run_id: str):
    try:
        _,_,md,_=_build_demo_bug_report(run_id)
        return send_file(md,mimetype="text/markdown; charset=utf-8",as_attachment=True,download_name=f"MESFlow-QA-{run_id}-GPT-Bug-Report.md")
    except Exception as exc:
        return jsonify({"ok":False,"error":"BUG_REPORT_FAILED","message":_redact_bug_text(exc)}),500


@app.get("/api/demo/<run_id>/bug-bundle.zip")
def api_demo_bug_bundle(run_id: str):
    try:
        _,_,_,bundle=_build_demo_bug_report(run_id)
        return send_file(bundle,mimetype="application/zip",as_attachment=True,download_name=f"MESFlow-QA-{run_id}-GPT-Bug-Bundle.zip")
    except Exception as exc:
        return jsonify({"ok":False,"error":"BUG_REPORT_FAILED","message":_redact_bug_text(exc)}),500


@app.get("/api/demo/<run_id>/state")
def api_demo_state(run_id: str):
    state=_runs.get(run_id)
    if not state or state.test_type!="demo": return jsonify({"ok":False,"error":"DEMO_RUN_NOT_FOUND"}),404
    path=REPORT_DIR / run_id / "demo" / "state.json"
    if not path.exists(): return jsonify({"ok":True,"state":{}})
    try: data=json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc: return jsonify({"ok":False,"error":str(exc)}),500
    return jsonify({"ok":True,"state":data})

@app.get("/api/demo/<run_id>/live.png")
def api_demo_live(run_id: str):
    state=_runs.get(run_id)
    if not state or state.test_type!="demo": return make_response("",404)
    path=REPORT_DIR / run_id / "demo" / "live.png"
    if not path.exists(): return make_response("",404)
    response=send_file(path,mimetype="image/png",conditional=False,max_age=0)
    response.headers["Cache-Control"]="no-store, max-age=0"
    return response

@app.post("/api/demo/<run_id>/control")
def api_demo_control(run_id: str):
    state=_runs.get(run_id)
    if not state or state.test_type!="demo": return jsonify({"ok":False,"error":"DEMO_RUN_NOT_FOUND"}),404
    path=REPORT_DIR / run_id / "demo" / "control.json"
    try: control=json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"pause_requested":False,"next_seq":0}
    except Exception: control={"pause_requested":False,"next_seq":0}
    action=str((request.get_json(silent=True) or {}).get("action") or "").lower()
    if action=="pause": control["pause_requested"]=True
    elif action in {"resume","next"}:
        control["pause_requested"]=False; control["next_seq"]=int(control.get("next_seq") or 0)+1
    else: return jsonify({"ok":False,"error":"INVALID_DEMO_ACTION"}),400
    path.write_text(json.dumps(control,ensure_ascii=False,indent=2),encoding="utf-8")
    return jsonify({"ok":True,"control":control})

@app.get("/api/demo/<run_id>/screenshots")
def api_demo_screenshots(run_id: str):
    state=_runs.get(run_id)
    if not state or state.test_type!="demo": return jsonify({"ok":False,"error":"DEMO_RUN_NOT_FOUND"}),404
    folder=REPORT_DIR / run_id / "demo" / "screenshots"
    items=[]
    if folder.exists():
        for p in sorted(folder.glob("*.png"),key=lambda x:x.stat().st_mtime):
            items.append({"name":p.name,"label":p.stem,"mtime":p.stat().st_mtime})
    return jsonify({"ok":True,"items":items})

@app.get("/api/demo/<run_id>/screenshots/<name>")
def api_demo_screenshot(run_id: str,name: str):
    state=_runs.get(run_id)
    if not state or state.test_type!="demo": return make_response("",404)
    safe=Path(name).name
    if safe!=name or not safe.endswith(".png"): return make_response("",400)
    path=REPORT_DIR / run_id / "demo" / "screenshots" / safe
    if not path.exists(): return make_response("",404)
    response=send_file(path,mimetype="image/png",conditional=False,max_age=0); response.headers["Cache-Control"]="no-store, max-age=0"; return response

@app.post("/api/cleanup")
def api_cleanup():
    return jsonify({
        "ok": False,
        "error": "LEGACY_QA_CLEANUP_DISABLED",
        "message": "Cleanup theo prefix đã bị tắt. Dùng QA Database riêng và Reset từ Golden Template."
    }), 410


@app.get("/api/database/reset/preview")
def api_database_reset_preview():
    cfg=load_config()
    try:
        return jsonify(qa_database_reset_preview(_cleanup_database_url(cfg)))
    except Exception as exc:
        return jsonify({"ok":False,"error":f"{type(exc).__name__}: {exc}"}),500


@app.post("/api/database/reset")
def api_database_reset():
    cfg=load_config()
    payload=request.get_json(silent=True) or {}
    try:
        result=qa_database_reset_execute(_cleanup_database_url(cfg),str(payload.get("confirm") or ""))
        return jsonify(result),200
    except (ValueError,RuntimeError) as exc:
        return jsonify({"ok":False,"error":str(exc)}),409
    except Exception as exc:
        return jsonify({"ok":False,"error":f"{type(exc).__name__}: {exc}"}),500


@app.get("/api/database/demo/preview")
def api_database_demo_preview():
    cfg=load_config()
    try:
        return jsonify(demo_database_preview(_cleanup_database_url(cfg)))
    except Exception as exc:
        return jsonify({"ok":False,"error":f"{type(exc).__name__}: {exc}"}),500


@app.post("/api/database/demo/prepare")
def api_database_demo_prepare():
    cfg=load_config()
    payload=request.get_json(silent=True) or {}
    try:
        return jsonify(demo_database_prepare_execute(_cleanup_database_url(cfg),str(payload.get("confirm") or ""))),200
    except (ValueError,RuntimeError) as exc:
        return jsonify({"ok":False,"error":str(exc)}),409
    except Exception as exc:
        return jsonify({"ok":False,"error":f"{type(exc).__name__}: {exc}"}),500


@app.post("/api/database/demo/reset")
def api_database_demo_reset():
    cfg=load_config()
    payload=request.get_json(silent=True) or {}
    try:
        return jsonify(demo_database_reset_execute(_cleanup_database_url(cfg),str(payload.get("confirm") or ""))),200
    except (ValueError,RuntimeError) as exc:
        return jsonify({"ok":False,"error":str(exc)}),409
    except Exception as exc:
        return jsonify({"ok":False,"error":f"{type(exc).__name__}: {exc}"}),500


@app.get("/api/database/full-cleanup/preview")
def api_database_full_cleanup_preview():
    return jsonify(postgres_cleanup_preview("")),410


@app.post("/api/database/full-cleanup")
def api_database_full_cleanup():
    return jsonify({"ok":False,"error":"FULL_CLEANUP_DISABLED: dùng /api/database/reset"}),410


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


# ---------------------------------------------------------------------------
# UI Preview Lab / Regression Protection / Bug Center (v1.23.0)
#
# Thin Flask wrappers only -- all real logic lives in engine/*.py so this
# file doesn't grow into another 2000-line module for these three screens.
# ---------------------------------------------------------------------------

_preview_mgr = preview_manager_mod.PreviewManager.build()


def _no_store(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response


@app.get("/preview-lab")
def preview_lab_page():
    return _no_store(make_response(render_template("preview_lab.html", app_version=APP_VERSION)))


@app.get("/regression")
def regression_page():
    return _no_store(make_response(render_template("regression.html", app_version=APP_VERSION)))


@app.get("/bugs")
def bugs_page():
    return _no_store(make_response(render_template("bugs.html", app_version=APP_VERSION)))


@app.get("/api/preview/presets")
def api_preview_presets():
    return jsonify({"ok": True, "presets": [
        {"key": key, "description": spec.description} for key, spec in preview_presets.SPECS.items()
    ]})


def _with_runtime(env: dict) -> dict:
    """Merge in a live, read-only Docker snapshot (requirement 2: 'Làm mới'
    reflects real container/health state without mutating anything), plus
    the browser-facing URL so the frontend never has to hardcode a host."""
    env = dict(env)
    env["runtime"] = _preview_mgr.runtime_state(env["id"])
    try:
        env["base_url"] = _preview_mgr.base_url(env["id"])
    except Exception:  # noqa: BLE001 - display only, never fail the request over it
        env["base_url"] = ""
    return env


@app.get("/api/preview/environments")
def api_preview_list():
    try:
        return jsonify({"ok": True, "environments": [_with_runtime(e) for e in _preview_mgr.list()]})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/api/preview/environments")
def api_preview_create():
    body = request.get_json(silent=True) or {}
    preset = str(body.get("preset") or "").strip()
    image = str(body.get("image") or "").strip() or None
    try:
        preview_presets.validate(preset)
    except preview_presets.UnknownPresetError as exc:
        return jsonify({"ok": False, "error": "UNKNOWN_PRESET", "message": str(exc)}), 400
    try:
        env_id = _preview_mgr.begin_create(preset, image=image)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

    def worker():
        try:
            _preview_mgr.finish_create(env_id)
        except Exception:
            app.logger.exception("Preview create failed for %s", env_id)

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"ok": True, "environment": _preview_mgr.get(env_id)}), 202


@app.get("/api/preview/environments/<env_id>")
def api_preview_get(env_id):
    try:
        return jsonify({"ok": True, "environment": _with_runtime(_preview_mgr.get(env_id))})
    except preview_manager_mod.PreviewNotFoundError:
        return jsonify({"ok": False, "error": "NOT_FOUND"}), 404


@app.post("/api/preview/environments/<env_id>/start")
def api_preview_start(env_id):
    try:
        env = _preview_mgr.begin_start(env_id)
    except preview_manager_mod.PreviewNotFoundError:
        return jsonify({"ok": False, "error": "NOT_FOUND"}), 404
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

    def worker():
        try:
            _preview_mgr.finish_start(env_id)
        except Exception:
            app.logger.exception("Preview start failed for %s", env_id)

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"ok": True, "environment": env}), 202


@app.post("/api/preview/environments/<env_id>/stop")
def api_preview_stop(env_id):
    try:
        return jsonify({"ok": True, "environment": _preview_mgr.stop(env_id)})
    except preview_manager_mod.PreviewNotFoundError:
        return jsonify({"ok": False, "error": "NOT_FOUND"}), 404
    except preview_manager_mod.PreviewSafetyError as exc:
        return jsonify({"ok": False, "error": "PREVIEW_SAFETY_REFUSED", "message": str(exc)}), 409
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/api/preview/environments/<env_id>/reset")
def api_preview_reset(env_id):
    body = request.get_json(silent=True) or {}
    new_preset = str(body.get("preset") or "").strip() or None
    if new_preset is not None:
        try:
            preview_presets.validate(new_preset)
        except preview_presets.UnknownPresetError as exc:
            return jsonify({"ok": False, "error": "UNKNOWN_PRESET", "message": str(exc)}), 400
    try:
        env = _preview_mgr.get(env_id)
    except preview_manager_mod.PreviewNotFoundError:
        return jsonify({"ok": False, "error": "NOT_FOUND"}), 404
    if env["status"] != "READY":
        return jsonify({"ok": False, "error": "PREVIEW_NOT_READY", "status": env["status"]}), 409

    def worker():
        try:
            _preview_mgr.reset_reseed(env_id, preset=new_preset)
        except preview_manager_mod.PreviewSafetyError:
            raise
        except Exception:
            app.logger.exception("Preview reset/reseed failed for %s", env_id)

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"ok": True, "environment": _preview_mgr.get(env_id)}), 202


@app.delete("/api/preview/environments/<env_id>")
def api_preview_delete(env_id):
    try:
        _preview_mgr.delete(env_id)
        return jsonify({"ok": True})
    except preview_manager_mod.PreviewNotFoundError:
        return jsonify({"ok": False, "error": "NOT_FOUND"}), 404
    except preview_manager_mod.PreviewSafetyError as exc:
        return jsonify({"ok": False, "error": "PREVIEW_SAFETY_REFUSED", "message": str(exc)}), 409
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/api/preview/environments/<env_id>/coverage")
def api_preview_run_coverage(env_id):
    try:
        env = _preview_mgr.get(env_id)
    except preview_manager_mod.PreviewNotFoundError:
        return jsonify({"ok": False, "error": "NOT_FOUND"}), 404
    if env["status"] != "READY":
        return jsonify({"ok": False, "error": "PREVIEW_NOT_READY", "status": env["status"]}), 409

    def worker():
        try:
            coverage_runner.run_coverage(_preview_mgr, env_id, qa_center_version=APP_VERSION)
        except Exception:
            app.logger.exception("UI Coverage run failed for %s", env_id)

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"ok": True, "message": "UI Coverage run started"}), 202


@app.get("/api/preview/coverage/<run_id>")
def api_preview_coverage_status(run_id):
    row = qa_store.connect().execute(
        "SELECT * FROM coverage_runs WHERE run_id=?", (run_id,)
    ).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "NOT_FOUND"}), 404
    data = dict(row)
    data["checks"] = json.loads(data.pop("checks_json") or "[]")
    data["summary"] = json.loads(data.pop("summary_json") or "{}")
    return jsonify({"ok": True, "run": data})


@app.get("/api/preview/coverage")
def api_preview_coverage_list():
    preview_id = request.args.get("preview_id") or None
    conn = qa_store.connect()
    if preview_id:
        rows = conn.execute("SELECT * FROM coverage_runs WHERE preview_id=? ORDER BY started_at DESC", (preview_id,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM coverage_runs ORDER BY started_at DESC LIMIT 100").fetchall()
    runs = []
    for row in rows:
        data = dict(row)
        data["checks"] = json.loads(data.pop("checks_json") or "[]")
        data["summary"] = json.loads(data.pop("summary_json") or "{}")
        runs.append(data)
    return jsonify({"ok": True, "runs": runs})


@app.get("/api/preview/coverage/<run_id>/screenshot/<name>")
def api_preview_coverage_screenshot(run_id, name):
    # Evidence screenshots only (requirement 15) -- never an arbitrary file
    # read: both path segments are validated against a strict allowlist
    # pattern before ever touching the filesystem.
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", run_id) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}\.png", name):
        return jsonify({"ok": False, "error": "INVALID_PATH"}), 400
    path = coverage_runner.REPORT_DIR / run_id / name
    if not path.is_file():
        return jsonify({"ok": False, "error": "NOT_FOUND"}), 404
    return send_file(path, mimetype="image/png")


@app.get("/api/regression/features")
def api_regression_features():
    features = feature_registry.list_features()
    for f in features:
        f["regression_cases"] = regression_policy.regression_cases_for(f["key"])
    return jsonify({"ok": True, "features": features})


@app.post("/api/regression/impact")
def api_regression_impact():
    body = request.get_json(silent=True) or {}
    previous_commit = str(body.get("previous_commit") or "").strip()
    current_commit = str(body.get("current_commit") or "HEAD").strip()
    repo_root = Path(os.environ.get("MESFLOW_SOURCE_ROOT", str(ROOT.parent.parent)))
    if not previous_commit:
        return jsonify({"ok": False, "error": "MISSING_PREVIOUS_COMMIT"}), 400
    try:
        result = feature_registry.compute_impact(previous_commit, current_commit, repo_root=repo_root)
    except feature_registry.ImpactDetectionError as exc:
        return jsonify({"ok": False, "error": "IMPACT_DETECTION_FAILED", "message": str(exc)}), 500
    # Requirement 10: the UI needs more than a bare list of keys -- for each
    # stable feature this touched, show what regression coverage already
    # exists and whether a coverage gap is blocking it from going back to
    # STABLE, so "3 stable features affected" is actionable, not just a count.
    impacted_detail = []
    for key in result["impacted_features"]:
        feature = feature_registry.get_feature(key) or {}
        impacted_detail.append({
            "key": key,
            "regression_cases": regression_policy.regression_cases_for(key),
            "coverage_gap": bool(feature.get("coverage_gap")),
            "coverage_gap_detail": feature.get("coverage_gap_detail") or "[]",
        })
    return jsonify({"ok": True, **result, "impacted_detail": impacted_detail})


@app.get("/api/regression/plan")
def api_regression_plan():
    mode = str(request.args.get("mode") or regression_policy.FAST).strip().upper()
    if mode not in regression_policy.MODES:
        return jsonify({"ok": False, "error": "INVALID_MODE", "modes": list(regression_policy.MODES)}), 400
    impacted = [f["key"] for f in feature_registry.list_features() if f["needs_regression"]]
    plan = regression_policy.compute_run_plan(mode, impacted_features=impacted)
    return jsonify({"ok": True, **plan})


@app.get("/api/bugs/summary")
def api_bugs_summary():
    return jsonify({"ok": True, "counts": bug_store.counts_by_status()})


@app.get("/api/bugs")
def api_bugs_list():
    bugs = bug_store.list_bugs(
        feature=request.args.get("feature") or None,
        severity=request.args.get("severity") or None,
        status=request.args.get("status") or None,
        run_id=request.args.get("run_id") or None,
    )
    return jsonify({"ok": True, "bugs": bugs})


@app.get("/api/bugs/<bug_id>")
def api_bugs_get(bug_id):
    bug = bug_store.get_bug(bug_id)
    if not bug:
        return jsonify({"ok": False, "error": "NOT_FOUND"}), 404
    return jsonify({"ok": True, "bug": bug})


@app.post("/api/bugs/<bug_id>/fixing")
def api_bugs_mark_fixing(bug_id):
    try:
        return jsonify({"ok": True, "bug": bug_store.mark_fixing(bug_id)})
    except bug_store.BugStateError as exc:
        return jsonify({"ok": False, "error": "INVALID_TRANSITION", "message": str(exc)}), 409


@app.post("/api/bugs/<bug_id>/ready-for-verify")
def api_bugs_mark_ready(bug_id):
    try:
        return jsonify({"ok": True, "bug": bug_store.mark_ready_for_verify(bug_id)})
    except bug_store.BugStateError as exc:
        return jsonify({"ok": False, "error": "INVALID_TRANSITION", "message": str(exc)}), 409


@app.post("/api/bugs/<bug_id>/verify")
def api_bugs_verify(bug_id):
    body = request.get_json(silent=True) or {}
    passed = bool(body.get("passed"))
    test_case = str(body.get("test_case") or "").strip()
    try:
        bug = bug_store.verify(bug_id, passed, test_case=test_case)
    except bug_store.BugStateError as exc:
        return jsonify({"ok": False, "error": "INVALID_TRANSITION", "message": str(exc)}), 409
    # Requirement 9: once a fix verifies PASS, permanently attach the
    # (often newly created) regression test case to the feature, and let
    # that PASS count toward the feature's stability streak.
    if passed and bug.get("feature") and test_case:
        feature_registry.attach_regression_test_case(bug["feature"], test_case)
        feature_registry.record_result(bug["feature"], test_case, "PASS", run_id=bug.get("run_id", ""))
        feature_registry.clear_needs_regression(bug["feature"])
    return jsonify({"ok": True, "bug": bug})


@app.post("/api/bugs/<bug_id>/ignore")
def api_bugs_ignore(bug_id):
    try:
        return jsonify({"ok": True, "bug": bug_store.ignore(bug_id)})
    except bug_store.BugStateError as exc:
        return jsonify({"ok": False, "error": "NOT_FOUND", "message": str(exc)}), 404


# ---------------------------------------------------------------------------
# LONG_RUNNING_FACTORY_SIMULATION (Phase A) -- thin Flask wrappers only, all
# real logic in engine/simulation/*.py, same convention as the block above.
# ---------------------------------------------------------------------------
from engine.simulation.run_manager import RunManager as _SimRunManager  # noqa: E402

_sim_mgr = _SimRunManager()


@app.get("/simulations")
def simulations_page():
    return render_template("simulation.html", app_version=APP_VERSION)


@app.get("/qualifications")
def qualifications_page():
    return render_template("qualifications.html", app_version=APP_VERSION)


@app.post("/api/simulation/start")
def api_simulation_start():
    body = request.get_json(silent=True) or {}
    env_id = str(body.get("preview_id") or "").strip()
    if not env_id:
        return jsonify({"ok": False, "error": "PREVIEW_ID_REQUIRED",
                         "message": "Start a UI Preview Lab environment first, then point the simulation at it."}), 400
    try:
        env = _preview_mgr.get(env_id)
    except preview_manager_mod.PreviewNotFoundError:
        return jsonify({"ok": False, "error": "PREVIEW_NOT_FOUND"}), 404
    profile = str(body.get("profile") or "SMALL_FACTORY").strip().upper()
    duration_label = str(body.get("duration") or "8_HOURS").strip().upper()
    speed_label = str(body.get("speed") or "REAL_TIME").strip().upper()
    seed = body.get("seed")
    try:
        snapshot = _sim_mgr.start(
            base_url=_preview_mgr.internal_base_url(env_id), admin_password=env["admin_password"],
            profile=profile, duration_label=duration_label, speed_label=speed_label,
            seed=int(seed) if seed else None,
        )
    except RuntimeError as exc:
        return jsonify({"ok": False, "error": "RUN_ALREADY_ACTIVE", "message": str(exc)}), 409
    except Exception as exc:
        app.logger.exception("Simulation bootstrap failed")
        return jsonify({"ok": False, "error": "BOOTSTRAP_FAILED", "message": str(exc)}), 500
    return jsonify({"ok": True, "run": snapshot}), 202


@app.get("/api/simulation/status")
def api_simulation_status():
    snapshot = _sim_mgr.status()
    return jsonify({"ok": True, "run": snapshot})


@app.post("/api/simulation/stop")
def api_simulation_stop():
    body = request.get_json(silent=True) or {}
    reason = str(body.get("reason") or "manual stop from QA Center UI")
    try:
        snapshot = _sim_mgr.stop(reason)
    except RuntimeError as exc:
        return jsonify({"ok": False, "error": "NO_ACTIVE_RUN", "message": str(exc)}), 404
    return jsonify({"ok": True, "run": snapshot})


@app.get("/api/simulation/incidents")
def api_simulation_incidents():
    run_id = request.args.get("run_id") or None
    bugs = [b for b in bug_store.list_bugs(run_id=run_id) if b.get("feature", "").startswith("SIM:")]
    return jsonify({"ok": True, "incidents": bugs})


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
