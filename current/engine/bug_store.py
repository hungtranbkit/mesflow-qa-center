"""Bug Center persistence: dedup, occurrence tracking, and the mandatory
verify-before-resolve / resolved-reappears-as-regression state machine.

Status flow (requirement 6/8):

    OPEN -> FIXING -> READY_FOR_VERIFY --(verify PASS)--> RESOLVED
                            ^                    |
                            +---(verify FAIL)-----+

    RESOLVED, same fingerprint seen again -> REGRESSION
    any status -> IGNORED

There is deliberately no "mark resolved" entry point other than
``verify(passed=True)``: a bug can never be resolved without going through
a verify pass first.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

from . import fingerprint as fp
from . import qa_store

OPEN = "OPEN"
FIXING = "FIXING"
READY_FOR_VERIFY = "READY_FOR_VERIFY"
RESOLVED = "RESOLVED"
REGRESSION = "REGRESSION"
IGNORED = "IGNORED"

_MAX_EVIDENCE_HISTORY = 10


class BugStateError(Exception):
    """Raised when a status transition is attempted out of order."""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    try:
        evidence = json.loads(d.get("evidence_json") or "{}")
    except (TypeError, ValueError):
        evidence = {}
    d["evidence"] = evidence
    return d


def _new_bug_id(conn: sqlite3.Connection, when: datetime) -> str:
    stamp = when.strftime("%Y%m%d")
    prefix = f"BUG-{stamp}-"
    row = conn.execute(
        "SELECT COUNT(*) c FROM bugs WHERE bug_id LIKE ?", (prefix + "%",)
    ).fetchone()
    seq = int(row["c"]) + 1
    return f"{prefix}{seq:04x}"


def record_bug(
    *,
    feature: str,
    error_type: str,
    title: str,
    severity: str = "MEDIUM",
    expected: str = "",
    actual: str = "",
    url: str = "",
    api: str = "",
    evidence: dict[str, Any] | None = None,
    run_id: str = "",
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Create-or-dedup a bug. Returns the resulting bug row as a dict.

    Fingerprint = hash(feature + error type + normalized title + endpoint).
    If a bug with that fingerprint already exists:
      - status RESOLVED -> the bug regressed: status becomes REGRESSION
      - otherwise -> occurrences += 1, last_seen updated, latest evidence attached
    Otherwise a new bug is created with status OPEN.
    """
    conn = conn or qa_store.connect()
    endpoint = api or url
    print_ = fp.compute(feature, error_type, title, endpoint)
    now = _now()
    existing = conn.execute("SELECT * FROM bugs WHERE fingerprint=?", (print_,)).fetchone()
    ev = dict(evidence or {})
    ev["fingerprint"] = print_
    if existing is None:
        when = datetime.now()
        bug_id = _new_bug_id(conn, when)
        conn.execute(
            """INSERT INTO bugs(bug_id,fingerprint,feature,severity,type,title,status,
                expected,actual,url,api,evidence_json,run_id,first_seen,last_seen,occurrences)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
            (bug_id, print_, feature, severity, error_type, title, OPEN,
             expected, actual, url, api, json.dumps({"history": [ev]}, ensure_ascii=False, default=str),
             run_id, now, now),
        )
        conn.commit()
        return get_bug(bug_id, conn=conn)  # type: ignore[return-value]

    bug_id = existing["bug_id"]
    history = []
    try:
        history = json.loads(existing["evidence_json"] or "{}").get("history", [])
    except (TypeError, ValueError):
        history = []
    history.append(ev)
    history = history[-_MAX_EVIDENCE_HISTORY:]
    new_status = existing["status"]
    if existing["status"] == RESOLVED:
        new_status = REGRESSION  # requirement: resolved bug reappearing is always a regression
    conn.execute(
        """UPDATE bugs SET occurrences=occurrences+1,last_seen=?,evidence_json=?,status=?,
               run_id=?,severity=?,url=COALESCE(NULLIF(?,''),url),api=COALESCE(NULLIF(?,''),api)
           WHERE bug_id=?""",
        (now, json.dumps({"history": history}, ensure_ascii=False, default=str), new_status,
         run_id, severity, url, api, bug_id),
    )
    conn.commit()
    return get_bug(bug_id, conn=conn)  # type: ignore[return-value]


def get_bug(bug_id: str, conn: sqlite3.Connection | None = None) -> dict[str, Any] | None:
    conn = conn or qa_store.connect()
    row = conn.execute("SELECT * FROM bugs WHERE bug_id=?", (bug_id,)).fetchone()
    return _row_to_dict(row) if row else None


def list_bugs(
    *, feature: str | None = None, severity: str | None = None, status: str | None = None,
    run_id: str | None = None, conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    conn = conn or qa_store.connect()
    where, params = [], []
    if feature:
        where.append("feature=?"); params.append(feature)
    if severity:
        where.append("severity=?"); params.append(severity)
    if status:
        where.append("status=?"); params.append(status)
    if run_id:
        where.append("run_id=?"); params.append(run_id)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(f"SELECT * FROM bugs{clause} ORDER BY last_seen DESC", params).fetchall()
    return [_row_to_dict(r) for r in rows]


def counts_by_status(conn: sqlite3.Connection | None = None) -> dict[str, int]:
    conn = conn or qa_store.connect()
    rows = conn.execute("SELECT status,COUNT(*) c FROM bugs GROUP BY status").fetchall()
    return {r["status"]: r["c"] for r in rows}


def _require_status(bug: dict[str, Any], *allowed: str) -> None:
    if bug["status"] not in allowed:
        raise BugStateError(
            f"Bug {bug['bug_id']} is {bug['status']}, expected one of {allowed}"
        )


def mark_fixing(bug_id: str, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    conn = conn or qa_store.connect()
    bug = get_bug(bug_id, conn=conn)
    if not bug:
        raise BugStateError(f"Unknown bug {bug_id}")
    _require_status(bug, OPEN, REGRESSION)
    conn.execute("UPDATE bugs SET status=? WHERE bug_id=?", (FIXING, bug_id))
    conn.commit()
    return get_bug(bug_id, conn=conn)  # type: ignore[return-value]


def mark_ready_for_verify(bug_id: str, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    conn = conn or qa_store.connect()
    bug = get_bug(bug_id, conn=conn)
    if not bug:
        raise BugStateError(f"Unknown bug {bug_id}")
    _require_status(bug, FIXING)
    conn.execute("UPDATE bugs SET status=? WHERE bug_id=?", (READY_FOR_VERIFY, bug_id))
    conn.commit()
    return get_bug(bug_id, conn=conn)  # type: ignore[return-value]


def verify(
    bug_id: str, passed: bool, *, test_case: str = "", conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """The only way a bug can become RESOLVED. Raises if not READY_FOR_VERIFY."""
    conn = conn or qa_store.connect()
    bug = get_bug(bug_id, conn=conn)
    if not bug:
        raise BugStateError(f"Unknown bug {bug_id}")
    _require_status(bug, READY_FOR_VERIFY)
    now = _now()
    if passed:
        conn.execute(
            "UPDATE bugs SET status=?,resolved_at=?,verify_result=?,regression_test_case=COALESCE(NULLIF(?,''),regression_test_case) WHERE bug_id=?",
            (RESOLVED, now, "PASS", test_case, bug_id),
        )
    else:
        conn.execute(
            "UPDATE bugs SET status=?,verify_result=? WHERE bug_id=?",
            (OPEN, "FAIL", bug_id),
        )
    conn.commit()
    return get_bug(bug_id, conn=conn)  # type: ignore[return-value]


def ignore(bug_id: str, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    conn = conn or qa_store.connect()
    bug = get_bug(bug_id, conn=conn)
    if not bug:
        raise BugStateError(f"Unknown bug {bug_id}")
    conn.execute("UPDATE bugs SET status=? WHERE bug_id=?", (IGNORED, bug_id))
    conn.commit()
    return get_bug(bug_id, conn=conn)  # type: ignore[return-value]
