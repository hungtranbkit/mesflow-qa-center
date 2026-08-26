"""Deterministic DB seed for the UI Preview Lab (requirement 1/12).

Writes directly to the target Postgres database -- but ONLY after
``preview_guard`` confirms the database name starts with ``mesflow_ui_``.
Every timestamp is generated relative to ``anchor`` (default: now), never
random noise, so a reviewer can reason about exactly what they're looking
at (e.g. "normal session = now - 75 minutes").

Scope note: this seeds one Part + one Operation per Production Order (not
the full multi-operation dependency graph) -- enough for Dashboard/Reports/
UI review, which is what UI Preview Lab is for. Full business-rule
correctness (multi-operation flows, QC, material consumption ledgers) is
Functional QA's job, not this module's (requirement 13: the two lanes are
deliberately separate).
"""
from __future__ import annotations

import random
import statistics
import uuid
from datetime import date, datetime, timedelta
from typing import Any

try:
    import psycopg
except ImportError:  # pragma: no cover - exercised only when psycopg is absent
    psycopg = None

from . import expectations as preview_expectations
from . import presets as preview_presets
from ..preview_guard import assert_preview_database_url

_VIETNAMESE_NAMES = [
    "Nguyễn Văn An", "Trần Thị Bình", "Lê Văn Cường", "Phạm Thị Dung",
    "Hoàng Văn Em", "Vũ Thị Giang", "Đặng Văn Hùng", "Bùi Thị Yến",
    "Đỗ Văn Khoa", "Ngô Thị Lan", "Dương Văn Minh", "Lý Thị Nga",
    "Phan Văn Oánh", "Tôn Thị Phương", "Đinh Văn Quang", "Mai Thị Sương",
    # Extended to 25 unique names (was 16) so a 25-employee preset never
    # has to repeat a name.
    "Trương Văn Tâm", "Võ Thị Uyên", "Đặng Văn Vinh", "Bùi Thị Xuân",
    "Lâm Văn Ý", "Châu Thị Ánh", "Huỳnh Văn Bảo", "Cao Thị Cẩm",
    "Lưu Văn Đạt",
]

# Operation-type catalog for the productivity-realistic generators
# (REPORT_30_DAYS, and FULL_UI's standard_seconds_per_unit): name, standard
# seconds/unit range, and the department that owns it. standard_seconds_per_unit
# is fixed per *type* (the midpoint of the range), not randomized per
# instance -- "Có thể dùng deterministic fixed values theo operation type".
_OPERATION_TYPES: tuple[tuple[str, int, int, str], ...] = (
    ("Cắt laser", 35, 55, "Cắt laser"),
    ("Làm nguội", 50, 80, "Cắt laser"),
    ("Dập", 20, 40, "Chấn"),
    ("Chấn CNC", 60, 90, "Chấn"),
    ("Hàn MIG", 140, 220, "Hàn"),
    ("Mài", 70, 110, "Hàn"),
    ("Lắp ráp", 90, 160, "Lắp ráp"),
    ("Kiểm tra QC", 45, 75, "QC"),
    ("Hoàn thiện", 90, 140, "Hoàn thiện"),
)


def _standard_seconds_for(op_type_index: int) -> float:
    name, lo, hi, _dept = _OPERATION_TYPES[op_type_index % len(_OPERATION_TYPES)]
    return float((lo + hi) // 2)


_DEPARTMENTS = ("Cắt laser", "Chấn", "Hàn", "Hoàn thiện", "Lắp ráp", "QC")

# Per-operation-type realistic units/session (2026-08-23 realism pass: keyed
# by _OPERATION_TYPES' own name so it never drifts out of sync with that
# table). These are the *common-case* range -- see _qty_from_target_duration
# for why a minority of sessions are allowed outside it.
_OPERATION_QTY_RANGES: dict[str, tuple[int, int]] = {
    "Cắt laser": (20, 120),
    "Làm nguội": (10, 80),     # finishing-adjacent (post-laser cooldown/deburr)
    "Dập": (50, 300),          # stamping
    "Chấn CNC": (15, 100),     # bending
    "Hàn MIG": (5, 40),        # welding
    "Mài": (10, 60),           # grinding
    "Lắp ráp": (5, 50),        # assembly
    "Kiểm tra QC": (20, 150),  # QC
    "Hoàn thiện": (10, 80),    # finishing
}

# Session-duration realism (2026-08-23 pass, replacing the old
# qty-first/duration-derived approach that produced mostly 5-15 minute
# sessions). Weighted minute bands a *target* duration is sampled from
# BEFORE any quantity is chosen -- see _qty_from_target_duration.
_DURATION_BANDS_MINUTES: tuple[tuple[int, int, float], ...] = (
    (20, 45, 0.10), (45, 90, 0.25), (90, 180, 0.40), (180, 300, 0.20), (300, 420, 0.05),
)

# Realistic factory work windows a session's start time is drawn from --
# morning shift, afternoon shift, and a smaller optional-overtime slice.
# Deliberately excludes the small hours (no 02:00 starts) unless a preset
# explicitly wants night-shift coverage, which REPORT_30_DAYS does not.
_WORK_WINDOWS_MINUTES: tuple[tuple[int, int, float], ...] = (
    (7 * 60, 11 * 60 + 30, 0.45),        # 07:00-11:30
    (13 * 60, 17 * 60, 0.45),            # 13:00-17:00
    (17 * 60 + 30, 20 * 60 + 30, 0.10),  # optional overtime 17:30-20:30
)

# One worked-hours TARGET per employee over the 30-day window (25 values,
# deterministic, hand-chosen like _EMPLOYEE_BASELINE_PRODUCTIVITY): 3 high
# performers reaching toward 130-150h, 17 in the "typical" 90-120h band, 5
# lower-utilization employees around 60-80h. This is a *target* that
# per-session duration sampling is scaled to hit (see _seed_report_30_days) --
# the realized total lands close to it, not exactly on it, once quantities
# are rounded to whole units and softly bounded to each operation type's
# realistic range.
_EMPLOYEE_TARGET_HOURS: tuple[int, ...] = (
    148, 140, 133, 120, 118, 116, 114, 112, 111, 109,
    107, 105, 103, 101, 99, 98, 96, 94, 92, 90,
    78, 74, 70, 66, 62,
)


def _sample_duration_seconds(rng: random.Random) -> float:
    """A realistic TARGET session duration, sampled BEFORE any quantity is
    chosen (task requirement: never generate a small qty first and derive
    a tiny duration from it). ~10% short sessions (interrupted work/rework/
    quick inspection/setup), ~65% in the 45-180 min "normal work block"
    range, ~25% long sessions (180-420 min) -- median lands around 90-150
    minutes once combined with the per-employee scaling below."""
    r = rng.random()
    acc = 0.0
    for lo, hi, weight in _DURATION_BANDS_MINUTES:
        acc += weight
        if r <= acc:
            return rng.uniform(lo, hi) * 60.0
    return rng.uniform(*_DURATION_BANDS_MINUTES[-1][:2]) * 60.0


def _sample_start_minutes(rng: random.Random) -> int:
    """Minutes-since-midnight for a session start, drawn from realistic
    factory shift windows (07:00-11:30 / 13:00-17:00 / optional overtime
    17:30-20:30) -- never the small hours, unlike the old flat 6:00-16:00
    window this replaces."""
    r = rng.random()
    acc = 0.0
    for lo, hi, weight in _WORK_WINDOWS_MINUTES:
        acc += weight
        if r <= acc:
            return rng.randint(lo, hi)
    return rng.randint(*_WORK_WINDOWS_MINUTES[-1][:2])


def _qty_from_target_duration(
    std: float, target_duration_seconds: float, target_pct: float, qty_range: tuple[int, int],
) -> int:
    """The task's own duration-first algorithm, inverted from the old
    qty-first one that this replaces: given a realistic TARGET session
    duration (sampled from _sample_duration_seconds) and this session's own
    target productivity %, derive how many units that implies, then round
    to a whole unit. Quantity is always the thing CALCULATED here, never
    the thing chosen first:

        expected_seconds = target_duration_seconds * (target_pct / 100)
        qty = round(expected_seconds / standard_seconds_per_unit)

    Softly bounded to the operation type's realistic range: floored at 1/4
    of its stated minimum (never a degenerate near-zero-unit session) and
    allowed up to 3x its stated maximum. The stated ranges are the *common*
    case, not a hard ceiling -- 25 employees each needing 30-45 sessions to
    add up to 70-150 worked hours over 30 days requires some sessions on
    high-throughput operations (stamping, QC, finishing) to run a larger
    batch than "typical", exactly like a real line absorbing a big order.
    """
    expected_seconds = target_duration_seconds * (target_pct / 100.0)
    qty = round(expected_seconds / std) if std > 0 else 1
    qty_lo, qty_hi = qty_range
    floor = max(1, qty_lo // 4)
    ceiling = qty_hi * 3
    return max(floor, min(ceiling, qty))

# One baseline productivity %% per employee (25 values), deterministic and
# hand-chosen to land in the requested overall distribution once per-session
# jitter (+/-3-8%%) is applied: 2 <60%% (8%%), 4 in 60-79%% (16%%), 11 in
# 80-99%% (44%%), 6 in 100-115%% (24%%), 2 >115%% (8%%) -- see requirement's
# "<60%: 5-10%, 60-79%: 15-20%, 80-99%: 40-50%, 100-115%: 20-30%, >115%: 5%".
_EMPLOYEE_BASELINE_PRODUCTIVITY: tuple[int, ...] = (
    122, 118, 113, 110, 108, 105, 103, 101, 99, 98,
    97, 96, 95, 93, 91, 90, 88, 85, 82, 76,
    72, 68, 62, 58, 55,
)

_RUNTIME_TABLES_TO_WIPE = (
    "session_exception_reviews", "operation_adjustments", "qc_inspections",
    "penalty_tickets", "work_sessions", "operations", "parts",
    "production_orders", "sales_orders", "equipment", "stations", "employees",
)


def wipe_runtime_data(database_url: str) -> None:
    """Reset a preview database back to blank runtime state before re-seeding.
    Refuses (fail closed) unless the database name starts with mesflow_ui_."""
    assert_preview_database_url(database_url)
    if psycopg is None:
        raise RuntimeError("psycopg is required to seed/reset a preview database")
    with psycopg.connect(database_url, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute(f"TRUNCATE TABLE {', '.join(_RUNTIME_TABLES_TO_WIPE)} RESTART IDENTITY CASCADE")
        conn.commit()


def _seed_employees(cur, n: int, edge_cases: bool, departments: tuple[str, ...] | None = None) -> list[int]:
    """`departments`, when given, cycles employees across real department
    names (task: "department/team hợp lý") instead of the single flat
    "Sản xuất" every preset used before -- optional so FULL_UI/legacy
    presets keep their existing behavior unchanged."""
    ids = []
    for i in range(n):
        if edge_cases and i == 0:
            name = "Nguyễn Thị Rất-Dài-Tên-Để-Kiểm-Tra-Không-Vỡ-Layout 测试文字 🏭"
        else:
            name = _VIETNAMESE_NAMES[i % len(_VIETNAMESE_NAMES)]
        department = departments[i % len(departments)] if departments else "Sản xuất"
        cur.execute(
            """INSERT INTO employees(employee_no,name,department,team,position,employment_status,active,qr)
               VALUES(%s,%s,%s,%s,%s,%s,true,%s) RETURNING id""",
            (f"QA-EMP-{i + 1:03d}", name, department, f"Ca {1 + i % 2}", "Công nhân", "Đang làm", uuid.uuid4().hex),
        )
        ids.append(cur.fetchone()[0])
    return ids


def _seed_stations(cur, n: int) -> list[int]:
    ids = []
    for i in range(n):
        cur.execute(
            """INSERT INTO stations(code,name,workshop,production_line,active)
               VALUES(%s,%s,%s,%s,true) RETURNING id""",
            (f"QA-ST-{i + 1:02d}", f"Trạm {i + 1:02d}", "Xưởng 1", "Line A"),
        )
        ids.append(cur.fetchone()[0])
    return ids


def _seed_sales_order(cur) -> int:
    cur.execute(
        """INSERT INTO sales_orders(code,customer_name,contract_no,status,priority)
           VALUES(%s,%s,%s,%s,%s) RETURNING id""",
        ("QA-SO-PREVIEW", "UI Preview Lab Customer", "", "CONFIRMED", "NORMAL"),
    )
    return cur.fetchone()[0]


def _insert_po_with_operation(
    cur, *, code: str, sales_order_id: int, status: str, due_date: date | None,
    created_at: datetime, planned_quantity: int, done_qty: int, defect_qty: int, rework_qty: int,
    operation_name: str = "Công đoạn hoàn thiện", standard_seconds_per_unit: float = 60.0,
) -> dict[str, Any]:
    cur.execute(
        """INSERT INTO production_orders(code,sales_order_id,product,planned_quantity,status,priority,
               due_date,notes,created_at,updated_at)
           VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (code, sales_order_id, f"Sản phẩm {code}", planned_quantity, status, "NORMAL",
         due_date, "", created_at, created_at),
    )
    po_id = cur.fetchone()[0]
    cur.execute(
        """INSERT INTO parts(production_order_id,code,name,sort_order)
           VALUES(%s,%s,%s,0) RETURNING id""",
        (po_id, "P1", f"Chi tiết {code}"),
    )
    part_id = cur.fetchone()[0]
    op_status = "COMPLETED" if status == "COMPLETED" else ("IN_PROGRESS" if done_qty or defect_qty else "PLANNED")
    # `operations` has no per-operation planned-quantity column -- planned
    # quantity lives only on production_orders (see the INSERT above). A
    # previous version of this INSERT referenced such a column; found live
    # by the Docker smoke test, every preset except EMPTY_STATE (which never
    # reaches this INSERT) crash-failed the seed step until this fix.
    #
    # standard_seconds_per_unit defaults to 60.0, never 0/NULL: MESFlow's own
    # employee-performance report computes efficiency_percent as
    # expected_seconds/actual_seconds*100 where expected_seconds =
    # standard_seconds_per_unit * qty -- a zero here makes expected_seconds
    # always 0, which the report treats as "not enough data" and renders as
    # NULL for every single session, no matter how much real session history
    # exists. Confirmed live: this was happening for every previously-seeded
    # operation before this fix.
    cur.execute(
        """INSERT INTO operations(production_order_id,part_id,code,name,done_qty,defect_qty,
               rework_qty,status,sort_order,qr,standard_seconds_per_unit,created_at,updated_at)
           VALUES(%s,%s,%s,%s,%s,%s,%s,%s,0,%s,%s,%s,%s) RETURNING id""",
        (po_id, part_id, f"{code}-OP-1", operation_name, done_qty,
         defect_qty, rework_qty, op_status, uuid.uuid4().hex, standard_seconds_per_unit, created_at, created_at),
    )
    op_id = cur.fetchone()[0]
    return {"po_id": po_id, "part_id": part_id, "operation_id": op_id, "code": code}


def _insert_session(
    cur, *, employee_id: int, operation_id: int, station_id: int, status: str,
    started_at: datetime, ended_at: datetime | None, good_qty: int, defect_qty: int, rework_qty: int,
) -> int:
    cur.execute(
        """INSERT INTO work_sessions(employee_id,operation_id,station_id,device_uuid,status,started_at,
               ended_at,good_qty,defect_qty,rework_qty,note,start_request_id,finish_request_id,created_at,updated_at)
           VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (employee_id, operation_id, station_id, "qa-preview-kiosk", status, started_at, ended_at,
         good_qty, defect_qty, rework_qty, "", uuid.uuid4().hex,
         uuid.uuid4().hex if status == "CLOSED" else None, started_at, ended_at or started_at),
    )
    return cur.fetchone()[0]


_DURATION_BUCKETS_MINUTES = ((20, 45), (45, 90), (90, 180))
_LONG_DURATION_MINUTES = (300, 540)  # "a few unusually long sessions"

# (category, due_offset_days, planned_qty, done_qty, defect_qty, band).
# category drives PO status/due_date exactly like the old per-status loops
# (late/warning/completed/on_track -- same dashboard semantics); band is
# purely informational, used to build the expectations manifest's
# progress-distribution assertion. Every number here is explicit and
# hand-chosen (never randomly generated) so a reviewer can see exactly why
# each operation sits where it does -- 20 operations: 8 on_track, 2 late,
# 3 warning, 7 completed; 12 "partial" (10-99%), 6 at 100%, 1 at >100%
# (over-production edge case), 1 at 0% (not started).
_FULL_UI_OPERATION_PLAN: tuple[tuple[str, int, int, int, int, str], ...] = (
    ("on_track", 25, 120, 0, 0, "0%"),
    ("on_track", 20, 100, 12, 1, "10-20%"),
    ("late", -6, 140, 22, 2, "10-20%"),
    ("on_track", 16, 180, 34, 2, "10-20%"),
    ("late", -9, 120, 40, 2, "30-50%"),
    ("on_track", 12, 150, 62, 3, "30-50%"),
    ("on_track", 10, 200, 96, 4, "30-50%"),
    ("warning", 0, 100, 65, 2, "60-80%"),
    ("on_track", 6, 160, 115, 4, "60-80%"),
    ("on_track", 4, 220, 172, 5, "60-80%"),
    ("warning", 0, 90, 83, 2, "90-99%"),
    ("warning", 0, 140, 134, 3, "90-99%"),
    ("on_track", 2, 200, 198, 4, "90-99%"),
    ("completed", -1, 80, 80, 1, "100%"),
    ("completed", -3, 100, 100, 2, "100%"),
    ("completed", -6, 120, 120, 3, "100%"),
    ("completed", -10, 150, 150, 4, "100%"),
    ("completed", -16, 180, 180, 4, "100%"),
    ("completed", -22, 90, 90, 2, "100%"),
    ("completed", -2, 100, 112, 3, ">100%"),
)

_DUE_STATUS = {"late": "IN_PROGRESS", "warning": "IN_PROGRESS", "on_track": "IN_PROGRESS", "completed": "COMPLETED"}


def _split_into_chunks(rng: random.Random, total: int, min_chunk: int = 6, max_chunk: int = 15) -> list[int]:
    """Split `total` units into realistic per-session batch sizes -- never
    one monolithic chunk, never truly random (rng is always the
    preset-seeded Random, so this is deterministic and reproducible)."""
    if total <= 0:
        return []
    chunks: list[int] = []
    remaining = total
    while remaining > 0:
        take = min(remaining, rng.randint(min_chunk, max_chunk))
        if 0 < remaining - take < min_chunk:
            take = remaining  # fold a too-small remainder into this chunk
        chunks.append(take)
        remaining -= take
    return chunks


def _pick_duration_minutes(rng: random.Random, session_index: int) -> int:
    if session_index and session_index % 17 == 0:
        return rng.randint(*_LONG_DURATION_MINUTES)  # "vài session dài bất thường"
    lo, hi = rng.choice(_DURATION_BUCKETS_MINUTES)
    return rng.randint(lo, hi)


def _seed_realistic_operations(
    cur, rng: random.Random, *, anchor: datetime, sales_order_id: int,
    employee_ids: list[int], station_ids: list[int], preset: str,
) -> dict[str, Any]:
    """FULL_UI's factory-scale generator (requirement: Preview must look
    like a real running factory). Builds ~20 operations deliberately spread
    across every completion-%% band, each with real multi-session work
    history whose SUM(good_qty)/SUM(defect_qty) exactly equals the
    operation's own done_qty/defect_qty -- session history and the operation
    aggregate can never disagree, because the aggregate IS the sum of the
    sessions, not a second independently-guessed number."""
    session_counter = 0
    operations: list[dict[str, Any]] = []
    band_counts: dict[str, int] = {}
    category_counts: dict[str, int] = {}
    completed_session_count = 0

    for op_index, (category, due_offset, planned_qty, done_qty, defect_qty, band) in enumerate(_FULL_UI_OPERATION_PLAN):
        due_dt = anchor + timedelta(days=due_offset)
        due_date = due_dt.date()
        created_at = min(due_dt, anchor) - timedelta(days=rng.randint(8, 15))
        code = f"{preset}-PO-{op_index + 1:04d}"
        po = _insert_po_with_operation(
            cur, code=code, sales_order_id=sales_order_id, status=_DUE_STATUS[category],
            due_date=due_date, created_at=created_at, planned_quantity=planned_qty,
            done_qty=done_qty, defect_qty=defect_qty, rework_qty=min(1, defect_qty),
        )
        operations.append({**po, "category": category, "band": band, "planned_qty": planned_qty,
                            "done_qty": done_qty, "defect_qty": defect_qty})
        band_counts[band] = band_counts.get(band, 0) + 1
        category_counts[category] = category_counts.get(category, 0) + 1

        # -- historical closed sessions, summing exactly to done_qty/defect_qty --
        lifetime_days = max(1, min((anchor.date() - created_at.date()).days, 30))
        good_chunks = _split_into_chunks(rng, done_qty)
        defect_remaining = defect_qty
        for chunk_i, good in enumerate(good_chunks):
            defect_here = defect_remaining if chunk_i == len(good_chunks) - 1 else 0
            defect_remaining -= defect_here
            day_offset = rng.randint(0, lifetime_days)
            duration_min = _pick_duration_minutes(rng, session_counter)
            started = anchor - timedelta(days=day_offset, minutes=rng.randint(0, 600))
            ended = started + timedelta(minutes=duration_min)
            employee_id = employee_ids[(op_index * 7 + chunk_i) % len(employee_ids)]
            station_id = station_ids[(op_index + chunk_i) % len(station_ids)]
            _insert_session(
                cur, employee_id=employee_id, operation_id=po["operation_id"], station_id=station_id,
                status="CLOSED", started_at=started, ended_at=ended, good_qty=good,
                defect_qty=defect_here, rework_qty=min(1, defect_here),
            )
            session_counter += 1
            completed_session_count += 1

    return {
        "operations": operations, "band_counts": band_counts, "category_counts": category_counts,
        "completed_session_count": completed_session_count, "session_counter": session_counter,
    }


def _seed_realistic_active_sessions(
    cur, rng: random.Random, *, anchor: datetime, operations: list[dict[str, Any]],
    employee_ids: list[int], station_ids: list[int],
) -> dict[str, Any]:
    """Layers "right now" state on top of the historical sessions above
    (requirement: 6-12 active sessions; employees split into đang làm /
    chờ-idle / history-only). Open sessions carry small provisional
    quantities that are deliberately NOT folded into operations.done_qty --
    an in-flight session's output isn't finalized until it closes, so the
    aggregate must not count it yet."""
    eligible_ops = [o for o in operations if o["category"] != "completed" and o["done_qty"] > 0] or \
        [o for o in operations if o["category"] != "completed"]

    working_employee_ids = employee_ids[:10]   # "8-12 đang làm"
    idle_employee_ids = employee_ids[10:14]    # "3-5 chờ / idle"

    active_session_count = 0
    for i, employee_id in enumerate(working_employee_ids):
        op = eligible_ops[i % len(eligible_ops)]
        started = anchor - timedelta(minutes=rng.randint(15, 150))
        _insert_session(
            cur, employee_id=employee_id, operation_id=op["operation_id"],
            station_id=station_ids[i % len(station_ids)], status="OPEN",
            started_at=started, ended_at=None,
            good_qty=rng.randint(3, 12), defect_qty=rng.choice([0, 0, 0, 1]), rework_qty=0,
        )
        active_session_count += 1

    for i, employee_id in enumerate(idle_employee_ids):
        op = eligible_ops[i % len(eligible_ops)]
        ended = anchor - timedelta(minutes=rng.randint(30, 90))
        started = ended - timedelta(minutes=rng.randint(5, 15))
        # Zero qty on purpose: "chờ / idle" means checked out again without
        # producing anything yet this shift -- never disturbs done_qty
        # consistency since a zero contributes nothing to any sum.
        _insert_session(
            cur, employee_id=employee_id, operation_id=op["operation_id"],
            station_id=station_ids[i % len(station_ids)], status="CLOSED",
            started_at=started, ended_at=ended, good_qty=0, defect_qty=0, rework_qty=0,
        )

    return {"active_session_count": active_session_count,
            "working_employees": len(working_employee_ids), "idle_employees": len(idle_employee_ids)}


_PRODUCTIVITY_FLOOR = 50.0
_PRODUCTIVITY_CEILING = 150.0


def _productivity_pct_for(rng: random.Random, employee_index: int) -> float:
    """Employee's baseline +/- session jitter, hard-clamped to
    [50, 150] -- deterministic (never true randomness: `rng` is always the
    preset-seeded Random), but every single session's target is guaranteed
    inside the range the task asks for, not just "usually"."""
    baseline = _EMPLOYEE_BASELINE_PRODUCTIVITY[employee_index % len(_EMPLOYEE_BASELINE_PRODUCTIVITY)]
    jitter = rng.uniform(-10, 10)
    return max(_PRODUCTIVITY_FLOOR, min(_PRODUCTIVITY_CEILING, baseline + jitter))


def _seed_productivity_session(
    cur, rng: random.Random, *, employee_id: int, employee_index: int, operation_id: int,
    station_id: int, standard_seconds_per_unit: float, good_qty: int, defect_qty: int, started_at: datetime,
    target_pct: float | None = None,
) -> tuple[int, float, datetime]:
    """The one place duration is derived FROM quantity and a target
    productivity, never generated independently of it (requirement: "KHÔNG
    seed duration và quantity độc lập random"):

        expected_seconds = standard_seconds_per_unit * (good_qty+defect_qty)
        actual_seconds    = expected_seconds / (target_pct / 100)
        ended_at          = started_at + actual_seconds

    `target_pct`, when given, is used as-is instead of drawing a fresh one
    from `_productivity_pct_for` -- callers that picked qty FROM a target
    duration+pct (the 2026-08-23 duration-first generator; see
    _qty_from_target_duration) must reuse that exact same pct here, not
    draw a second independent one from `rng` (which would both desync this
    session's actual duration from the one it was sized for, and shift
    every rng draw after it, breaking determinism reproducibility).

    Returns (session_id, the productivity %% this session was built to hit,
    ended_at) -- callers doing per-employee scheduling need ended_at back
    directly to advance that employee's own no-overlap cursor without a
    round-trip SELECT."""
    qty = good_qty + defect_qty
    if target_pct is None:
        target_pct = _productivity_pct_for(rng, employee_index)
    expected_seconds = standard_seconds_per_unit * qty
    actual_seconds = expected_seconds / (target_pct / 100.0) if expected_seconds > 0 else 60.0
    ended_at = started_at + timedelta(seconds=actual_seconds)
    sid = _insert_session(
        cur, employee_id=employee_id, operation_id=operation_id, station_id=station_id, status="CLOSED",
        started_at=started_at, ended_at=ended_at, good_qty=good_qty, defect_qty=defect_qty,
        rework_qty=min(1, defect_qty),
    )
    return sid, target_pct, ended_at


def _seed_report_30_days(
    cur, rng: random.Random, *, anchor: datetime, sales_order_id: int,
    employee_ids: list[int], station_ids: list[int], preset: str,
) -> dict[str, Any]:
    """REPORT_30_DAYS: the standard dataset for the employee-performance /
    productivity report. 25 employees (see presets.py), 8-15 POs, 20-40
    operations across the 9 real operation types (each with a realistic,
    type-fixed standard_seconds_per_unit). Every employee gets their own
    30-45 CLOSED sessions (~750-1125 total) scheduled with no self-overlap
    across the last 30 days -- some days doubled up, some skipped entirely
    -- plus 6-10 OPEN sessions.

    2026-08-23 realism pass: session duration is now the thing chosen
    FIRST (from _sample_duration_seconds' realistic minute-band
    distribution, scaled per employee to hit that employee's own
    _EMPLOYEE_TARGET_HOURS), with quantity CALCULATED from it via
    _qty_from_target_duration -- the exact inverse of the old
    qty-first/duration-derived approach, which produced mostly 5-15 minute
    sessions (real bug: "32 sessions, 5h39m total"). Every operation's own
    done_qty/defect_qty is backfilled AFTER all sessions are generated, as
    the exact sum of the sessions assigned to it -- the aggregate is never
    an independently-guessed number, so it can never disagree with its own
    session history (same invariant as before, just computed in the other
    direction).
    """
    ops_per_po = (2, 3, 3, 2, 3, 2, 3, 2, 3, 2, 3, 2)  # 12 POs (8-15), 30 operations (20-40)
    history_days = 30

    operations: list[dict[str, Any]] = []
    op_type_cursor = 0
    for po_i, n_ops in enumerate(ops_per_po):
        po_seq = po_i + 1
        created_at = anchor - timedelta(days=rng.randint(22, 34))
        status = "COMPLETED" if po_i % 4 == 3 else "IN_PROGRESS"
        due_date = ((anchor - timedelta(days=rng.randint(0, 5))) if status == "COMPLETED"
                    else (anchor + timedelta(days=rng.randint(3, 20)))).date()
        # A floor, not a target any more -- real done_qty is backfilled once
        # sessions exist (see below) and planned_quantity is bumped upward
        # to stay >= it, never the other way around.
        planned_quantity_floor = rng.randint(280, 520)
        code = f"{preset}-PO-{po_seq:04d}"
        cur.execute(
            """INSERT INTO production_orders(code,sales_order_id,product,planned_quantity,status,priority,
                   due_date,notes,created_at,updated_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (code, sales_order_id, f"Sản phẩm {code}", planned_quantity_floor, status, "NORMAL",
             due_date, "", created_at, created_at),
        )
        po_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO parts(production_order_id,code,name,sort_order) VALUES(%s,%s,%s,0) RETURNING id""",
            (po_id, "P1", f"Chi tiết {code}"),
        )
        part_id = cur.fetchone()[0]

        for op_j in range(n_ops):
            op_name, lo, hi, dept = _OPERATION_TYPES[op_type_cursor % len(_OPERATION_TYPES)]
            op_type_cursor += 1
            std = float((lo + hi) // 2)
            op_status = "COMPLETED" if status == "COMPLETED" else "IN_PROGRESS"
            # done_qty/defect_qty start at 0 -- real values are UPDATEd in
            # once this operation's actual sessions are known below.
            cur.execute(
                """INSERT INTO operations(production_order_id,part_id,code,name,done_qty,defect_qty,rework_qty,
                       status,sort_order,qr,standard_seconds_per_unit,created_at,updated_at)
                   VALUES(%s,%s,%s,%s,0,0,0,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (po_id, part_id, f"{code}-OP-{op_j + 1}", op_name,
                 op_status, op_j, uuid.uuid4().hex, std, created_at, created_at),
            )
            op_id = cur.fetchone()[0]
            operations.append({
                "operation_id": op_id, "po_id": po_id, "op_name": op_name, "po_status": status,
                "done_qty": 0, "defect_qty": 0, "planned_quantity_floor": planned_quantity_floor,
                "standard_seconds_per_unit": std, "department": dept, "created_at": created_at,
                "qty_range": _OPERATION_QTY_RANGES.get(op_name, (5, 100)),
            })

    # -- two deliberately-invalid operations: no standard time configured
    # (requirement: "1-2 operation/session thiếu standard time" to exercise
    # the "Không đủ dữ liệu" UI state). Kept to exactly 2 -- REPORT_30_DAYS
    # must stay >=95% valid overall.
    invalid_operations: list[dict[str, Any]] = []
    for k in range(2):
        created_at = anchor - timedelta(days=rng.randint(5, 15))
        code = f"{preset}-EDGE-{k + 1:04d}"
        cur.execute(
            """INSERT INTO production_orders(code,sales_order_id,product,planned_quantity,status,priority,
                   due_date,notes,created_at,updated_at) VALUES(%s,%s,%s,%s,'IN_PROGRESS','NORMAL',%s,%s,%s,%s)
               RETURNING id""",
            (code, sales_order_id, f"Sản phẩm {code}", 50, (anchor + timedelta(days=10)).date(), "", created_at, created_at),
        )
        po_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO parts(production_order_id,code,name,sort_order) VALUES(%s,%s,%s,0) RETURNING id""",
            (po_id, "P1", f"Chi tiết {code}"),
        )
        part_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO operations(production_order_id,part_id,code,name,done_qty,defect_qty,rework_qty,
                   status,sort_order,qr,standard_seconds_per_unit,created_at,updated_at)
               VALUES(%s,%s,%s,%s,%s,0,0,'IN_PROGRESS',0,%s,0,%s,%s) RETURNING id""",
            (po_id, part_id, f"{code}-OP-1", "Công đoạn thử nghiệm (chưa cấu hình định mức)", 20,
             uuid.uuid4().hex, created_at, created_at),
        )
        invalid_operations.append({"operation_id": cur.fetchone()[0], "created_at": created_at})

    # -- CLOSED sessions: exactly 30-45 per employee (~750-1125 total),
    # scheduled per employee with no self-overlap, duration-first (see the
    # function docstring and _qty_from_target_duration).
    #
    # Per employee:
    #  1. Sample this employee's session COUNT (30-45) and a raw target
    #     duration per session from the global minute-band distribution.
    #  2. Scale every raw duration by target_total_hours/raw_total so this
    #     employee's sessions sum close to their own _EMPLOYEE_TARGET_HOURS
    #     tier (low/typical/high utilization) -- this is what varies total
    #     worked hours per employee without distorting the overall band
    #     shape (the scale factor is usually close to 1.0; see the
    #     constant's own docstring for the math).
    #  3. Each session picks one of the 30 real operations at random, then
    #     derives its quantity from the scaled target duration + this
    #     session's own target productivity via _qty_from_target_duration
    #     (softly bounded to that operation TYPE's realistic qty range).
    #     Every session's actual good/defect qty accumulates into that
    #     operation's running total -- backfilled into the DB below, once
    #     every employee's sessions exist.
    #  4. Sessions are scheduled chronologically (oldest day first) across
    #     18-27 active days out of the last 30 -- some days doubled up,
    #     some skipped -- with a per-employee time cursor that only ever
    #     moves forward, so the same employee can never have two sessions
    #     overlap. Start times are drawn from realistic shift windows
    #     (07:00-11:30 / 13:00-17:00 / optional overtime 17:30-20:30).
    employee_targets = [rng.randint(30, 45) for _ in employee_ids]  # 25 values, sum in [750, 1125]
    realistic_ops = operations  # all 30 real ones (invalid_operations kept separate, see above)

    closed_session_ids: list[int] = []
    productivity_values: list[float] = []
    session_minutes: list[float] = []
    worked_seconds_by_employee: list[float] = []
    op_accum: dict[int, list[float]] = {op["operation_id"]: [0, 0] for op in realistic_ops}  # [good, defect]
    session_counter = 0
    WORK_GAP_MINUTES = 20  # changeover time between two sessions for the same employee

    for employee_index, employee_id in enumerate(employee_ids):
        n_sessions = employee_targets[employee_index]
        raw_durations = [_sample_duration_seconds(rng) for _ in range(n_sessions)]
        target_total_seconds = _EMPLOYEE_TARGET_HOURS[employee_index % len(_EMPLOYEE_TARGET_HOURS)] * 3600.0
        scale = target_total_seconds / sum(raw_durations)

        # 18-27 active days out of the last 30 -- the remainder are rest
        # days with zero sessions; days needing >1 session absorb the
        # difference between n_sessions and active_days.
        active_days = max(1, min(30, min(n_sessions, rng.randint(18, 27))))
        day_offsets = sorted(rng.sample(range(1, 31), k=active_days), reverse=True)  # oldest first
        per_day = [1] * active_days
        extra = n_sessions - active_days
        i = 0
        while extra > 0:
            per_day[i % active_days] += 1
            extra -= 1
            i += 1

        cursor = anchor - timedelta(days=31)  # this employee's next-available time; only ever moves forward
        employee_worked_seconds = 0.0
        slot_i = 0
        for day_i, day_offset in enumerate(day_offsets):
            day_date = (anchor - timedelta(days=day_offset)).date()
            for _ in range(per_day[day_i]):
                if slot_i >= n_sessions:
                    break
                target_duration = raw_durations[slot_i] * scale
                slot_i += 1

                op = realistic_ops[rng.randrange(len(realistic_ops))]
                target_pct = _productivity_pct_for(rng, employee_index)
                qty = _qty_from_target_duration(
                    op["standard_seconds_per_unit"], target_duration, target_pct, op["qty_range"],
                )
                defect = round(qty * rng.uniform(0.0, 0.05))
                good = max(1, qty - defect)  # a session must produce *something*

                desired = datetime.combine(day_date, datetime.min.time()) + timedelta(
                    minutes=_sample_start_minutes(rng))
                started = max(desired, cursor + timedelta(minutes=WORK_GAP_MINUTES))
                sid, pct, ended = _seed_productivity_session(
                    cur, rng, employee_id=employee_id, employee_index=employee_index,
                    operation_id=op["operation_id"], station_id=station_ids[session_counter % len(station_ids)],
                    standard_seconds_per_unit=op["standard_seconds_per_unit"],
                    good_qty=good, defect_qty=defect, started_at=started, target_pct=target_pct,
                )
                actual_seconds = (ended - started).total_seconds()
                cursor = ended
                closed_session_ids.append(sid)
                productivity_values.append(pct)
                session_minutes.append(actual_seconds / 60.0)
                employee_worked_seconds += actual_seconds
                op_accum[op["operation_id"]][0] += good
                op_accum[op["operation_id"]][1] += defect
                session_counter += 1
        worked_seconds_by_employee.append(employee_worked_seconds)

    # -- backfill every real operation's done_qty/defect_qty/planned_quantity
    # from the sessions actually assigned to it above -- the aggregate is
    # computed FROM the sessions, never an independently-guessed number, so
    # it can never disagree with SUM(good_qty)/SUM(defect_qty) over its own
    # history. planned_quantity only ever moves UP from its floor (task:
    # "It is acceptable for planned_quantity to increase if necessary").
    po_planned_totals: dict[int, int] = {}
    for op in realistic_ops:
        good_sum, defect_sum = op_accum[op["operation_id"]]
        good_sum, defect_sum = int(good_sum), int(defect_sum)
        op["done_qty"], op["defect_qty"] = good_sum, defect_sum
        cur.execute(
            "UPDATE operations SET done_qty=%s, defect_qty=%s, rework_qty=%s WHERE id=%s",
            (good_sum, defect_sum, min(1, defect_sum), op["operation_id"]),
        )
        if op["po_status"] == "COMPLETED":
            op_planned = good_sum + defect_sum
        else:
            op_planned = round((good_sum + defect_sum) * rng.uniform(1.05, 1.3))
        # planned_quantity lives on production_orders (PO-level), not per
        # operation -- a PO with 2-3 operations sums their contributions
        # rather than one operation's UPDATE clobbering another's.
        po_planned_totals[op["po_id"]] = po_planned_totals.get(op["po_id"], 0) + op_planned
    for po_id, done_total in po_planned_totals.items():
        cur.execute(
            "UPDATE production_orders SET planned_quantity=GREATEST(planned_quantity, %s) WHERE id=%s",
            (done_total, po_id),
        )

    # -- intentional invalid sessions (requirement: keep them few) --
    invalid_session_ids: list[int] = []
    for op in invalid_operations:
        sid = _insert_session(
            cur, employee_id=employee_ids[session_counter % len(employee_ids)], operation_id=op["operation_id"],
            station_id=station_ids[session_counter % len(station_ids)], status="CLOSED",
            started_at=anchor - timedelta(days=2, hours=1), ended_at=anchor - timedelta(days=2),
            good_qty=15, defect_qty=1, rework_qty=0,
        )
        invalid_session_ids.append(sid)
        session_counter += 1
    for _ in range(2):  # "1-3 session qty = 0"
        op = operations[session_counter % len(operations)]
        sid = _insert_session(
            cur, employee_id=employee_ids[session_counter % len(employee_ids)], operation_id=op["operation_id"],
            station_id=station_ids[session_counter % len(station_ids)], status="CLOSED",
            started_at=anchor - timedelta(days=1, hours=2), ended_at=anchor - timedelta(days=1, hours=1),
            good_qty=0, defect_qty=0, rework_qty=0,
        )
        invalid_session_ids.append(sid)
        session_counter += 1

    # -- OPEN sessions: 6-10, distinct employees (work_sessions has a real
    # UNIQUE(employee_id) WHERE status='OPEN' constraint -- reusing an
    # employee here would crash the seed, not just be unrealistic). --
    open_eligible_ops = [o for o in operations if o["done_qty"] > 0][:12]
    open_session_ids: list[int] = []
    open_employee_start = len(employee_ids) - 10  # use the *last* 10 employees, distinct from closed-history-heavy ones
    for i in range(7):
        employee_id = employee_ids[(open_employee_start + i) % len(employee_ids)]
        op = open_eligible_ops[i % len(open_eligible_ops)]
        started = anchor - timedelta(minutes=rng.randint(15, 180))
        sid = _insert_session(
            cur, employee_id=employee_id, operation_id=op["operation_id"],
            station_id=station_ids[i % len(station_ids)], status="OPEN",
            started_at=started, ended_at=None, good_qty=rng.randint(4, 14), defect_qty=rng.choice([0, 0, 0, 1]),
            rework_qty=0,
        )
        open_session_ids.append(sid)
    # one unusually-long-open exception (requirement: keep the dataset from
    # being distorted by it -- exactly one).
    long_open_employee = employee_ids[(open_employee_start + 7) % len(employee_ids)]
    long_op = open_eligible_ops[0]
    long_sid = _insert_session(
        cur, employee_id=long_open_employee, operation_id=long_op["operation_id"],
        station_id=station_ids[0], status="OPEN",
        started_at=anchor - timedelta(hours=26), ended_at=None, good_qty=40, defect_qty=2, rework_qty=1,
    )
    open_session_ids.append(long_sid)
    _insert_exception(cur, session_id=long_sid, code="SESSION_LONG_OPEN",
                       note="Session open > 24h (seeded for productivity report coverage)")

    worked_hours_by_employee = [s / 3600.0 for s in worked_seconds_by_employee]
    return {
        "operations": operations + invalid_operations,
        "production_orders": len(ops_per_po) + len(invalid_operations),
        "closed_session_ids": closed_session_ids, "invalid_session_ids": invalid_session_ids,
        "open_session_ids": open_session_ids,
        "productivity_values": productivity_values,
        "employee_targets": employee_targets,
        "min_closed_sessions_per_employee": min(employee_targets),
        "avg_productivity_percent": (sum(productivity_values) / len(productivity_values)) if productivity_values else None,
        "history_days": history_days,
        # Worked-hours realism (2026-08-23): the task's own required
        # acceptance criteria -- session count alone is no longer enough.
        "worked_hours_by_employee": worked_hours_by_employee,
        "min_worked_hours_per_employee": min(worked_hours_by_employee) if worked_hours_by_employee else None,
        "avg_worked_hours": (sum(worked_hours_by_employee) / len(worked_hours_by_employee)) if worked_hours_by_employee else None,
        "max_worked_hours": max(worked_hours_by_employee) if worked_hours_by_employee else None,
        "session_minutes": session_minutes,
        "median_session_minutes": statistics.median(session_minutes) if session_minutes else None,
        "p90_session_minutes": (sorted(session_minutes)[max(0, int(len(session_minutes) * 0.9) - 1)]
                                 if session_minutes else None),
    }


def _insert_exception(cur, *, session_id: int, code: str, note: str) -> None:
    cur.execute(
        """INSERT INTO session_exception_reviews(session_id,exception_code,exception_fingerprint,
               workflow_status,note)
           VALUES(%s,%s,%s,'NEW',%s)
           ON CONFLICT (session_id, exception_fingerprint) DO NOTHING""",
        (session_id, code, f"seed-{session_id}-{code}", note),
    )


def run_seed(database_url: str, preset: str, anchor: datetime | None = None) -> dict[str, Any]:
    """Guard, then seed a blank preview database deterministically. Returns
    the expected-state manifest (requirement 12)."""
    assert_preview_database_url(database_url)
    if psycopg is None:
        raise RuntimeError("psycopg is required to seed a preview database")
    spec = preview_presets.get_spec(preset)
    anchor = anchor or datetime.now()
    rng = random.Random(f"{preset}-seed")  # deterministic shuffling only, never random *values*

    needed_open = spec.sessions_open_normal + spec.sessions_long_open
    employee_count = max(spec.employees, needed_open + 1, 1)
    counts = {
        "po_late": spec.pos_late, "po_warning": spec.pos_warning, "po_completed": spec.pos_completed,
        "sessions_open": spec.sessions_open_normal + spec.sessions_long_open,
        "sessions_long_open": spec.sessions_long_open,
        "sessions_closed": spec.sessions_closed_history,
        "exceptions": spec.exceptions_minimum,
    }

    with psycopg.connect(database_url, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            departments = _DEPARTMENTS if spec.productivity_scale else None
            employee_ids = _seed_employees(cur, employee_count, spec.edge_cases, departments=departments)
            station_ids = _seed_stations(cur, max(spec.stations, 1))
            sales_order_id = _seed_sales_order(cur)
            po_seq = 0
            pos: list[dict[str, Any]] = []
            open_session_ids: list[int] = []

            if spec.productivity_scale:
                report = _seed_report_30_days(
                    cur, rng, anchor=anchor, sales_order_id=sales_order_id,
                    employee_ids=employee_ids, station_ids=station_ids, preset=preset,
                )
                closed = report["closed_session_ids"]
                invalid = report["invalid_session_ids"]
                pv = report["productivity_values"]
                counts.update({
                    "po_late": 0, "po_warning": 0, "po_completed": 0,
                    "sessions_open": len(report["open_session_ids"]), "sessions_long_open": 0,
                    "sessions_closed": len(closed) + len(invalid),
                    "employees": employee_count,
                    "production_orders": report["production_orders"],
                    "operations": len(report["operations"]),
                    "closed_sessions": len(closed) + len(invalid),
                    "open_sessions": len(report["open_session_ids"]),
                    "productivity_valid_sessions": len(closed),
                    "productivity_invalid_sessions": len(invalid),
                    "operations_with_standard_time": sum(
                        1 for o in report["operations"] if o.get("standard_seconds_per_unit", 0) > 0
                    ),
                    "history_days": report["history_days"],
                    "avg_productivity_percent_expected_range": [75, 110],
                    "min_closed_sessions_per_employee": report["min_closed_sessions_per_employee"],
                    "avg_productivity_percent": report["avg_productivity_percent"],
                    # Worked-hours realism (2026-08-23) -- see the task's own
                    # "TOTAL WORKED TIME VALIDATION" / "TESTS / MANIFEST" sections.
                    "min_worked_hours_per_employee": report["min_worked_hours_per_employee"],
                    "avg_worked_hours": report["avg_worked_hours"],
                    "max_worked_hours": report["max_worked_hours"],
                    "median_session_minutes": report["median_session_minutes"],
                    "p90_session_minutes": report["p90_session_minutes"],
                })
                open_session_ids = report["open_session_ids"]
                pos = report["operations"]
            elif spec.realistic_scale:
                realistic = _seed_realistic_operations(
                    cur, rng, anchor=anchor, sales_order_id=sales_order_id,
                    employee_ids=employee_ids, station_ids=station_ids, preset=preset,
                )
                active = _seed_realistic_active_sessions(
                    cur, rng, anchor=anchor, operations=realistic["operations"],
                    employee_ids=employee_ids, station_ids=station_ids,
                )
                cat = realistic["category_counts"]
                partial_ops = sum(v for k, v in realistic["band_counts"].items() if k not in ("0%", "100%", ">100%"))
                counts.update({
                    "po_late": cat.get("late", 0), "po_warning": cat.get("warning", 0),
                    "po_completed": cat.get("completed", 0),
                    "sessions_open": active["active_session_count"], "sessions_long_open": 0,
                    "sessions_closed": realistic["completed_session_count"],
                    "employees": employee_count,
                    "completed_sessions_min": realistic["completed_session_count"],
                    "active_sessions_min": active["active_session_count"],
                    "operations_with_partial_progress_min": partial_ops,
                    "operations_completed_min": realistic["band_counts"].get("100%", 0),
                    "progress_bands": realistic["band_counts"],
                })
                cur.execute("SELECT id FROM work_sessions WHERE status='OPEN' ORDER BY id")
                open_session_ids = [row[0] for row in cur.fetchall()]
                pos = realistic["operations"]
            else:
                pos, open_session_ids = _seed_legacy_scenario(
                    cur, rng, anchor=anchor, spec=spec, preset=preset,
                    sales_order_id=sales_order_id, employee_ids=employee_ids, station_ids=station_ids,
                )

            # Top up exceptions to the preset minimum even if the scenario
            # above didn't naturally produce enough (e.g. NORMAL_FACTORY asks
            # for 0, PROBLEM_FACTORY asks for more than long-open sessions
            # can carry on their own).
            cur.execute("SELECT COUNT(*) FROM session_exception_reviews")
            have = cur.fetchone()[0]
            extra_needed = max(spec.exceptions_minimum - have, 0)
            if extra_needed and open_session_ids:
                for j in range(extra_needed):
                    sid = open_session_ids[j % len(open_session_ids)]
                    _insert_exception(cur, session_id=sid, code=f"SEED_TOP_UP_{j}",
                                       note="Deterministic filler exception to satisfy preset minimum")

        conn.commit()

    return preview_expectations.build_manifest(preset, anchor, counts)


def _seed_legacy_scenario(
    cur, rng: random.Random, *, anchor: datetime, spec, preset: str,
    sales_order_id: int, employee_ids: list[int], station_ids: list[int],
) -> tuple[list[dict[str, Any]], list[int]]:
    """The original small-scale per-status generator, unchanged, still used
    by NORMAL_FACTORY/PROBLEM_FACTORY/EMPTY_STATE/EDGE_CASES (deliberately
    small). FULL_UI and REPORT_30_DAYS have their own dedicated generators
    (_seed_realistic_operations / _seed_report_30_days) above."""
    po_seq = 0
    pos: list[dict[str, Any]] = []
    open_session_ids: list[int] = []

    for _ in range(spec.pos_late):
        po_seq += 1
        is_zero_qty_edge = spec.edge_cases and po_seq == 1
        pos.append(_insert_po_with_operation(
            cur, code=f"{preset}-PO-{po_seq:04d}", sales_order_id=sales_order_id,
            status="IN_PROGRESS", due_date=(anchor - timedelta(days=2)).date(),
            created_at=anchor - timedelta(days=12), planned_quantity=0 if is_zero_qty_edge else 200,
            done_qty=0 if is_zero_qty_edge else 60,
            defect_qty=0 if is_zero_qty_edge else 5,
            rework_qty=0 if is_zero_qty_edge else 2,
        ))
    for _ in range(spec.pos_warning):
        po_seq += 1
        # due_date is a DATE column (no time-of-day); "due in 6 hours"
        # is represented as due today -- the closest this schema can
        # express "about to breach".
        pos.append(_insert_po_with_operation(
            cur, code=f"{preset}-PO-{po_seq:04d}", sales_order_id=sales_order_id,
            status="IN_PROGRESS", due_date=anchor.date(),
            created_at=anchor - timedelta(days=4), planned_quantity=150,
            done_qty=90, defect_qty=3, rework_qty=1,
        ))
    for i in range(spec.pos_completed):
        po_seq += 1
        days_ago = 1 + int((i / max(spec.pos_completed, 1)) * 29)  # spread across last 30 days
        due = anchor - timedelta(days=days_ago)
        pos.append(_insert_po_with_operation(
            cur, code=f"{preset}-PO-{po_seq:04d}", sales_order_id=sales_order_id,
            status="COMPLETED", due_date=due.date(), created_at=due - timedelta(days=3),
            planned_quantity=100, done_qty=100, defect_qty=4, rework_qty=2,
        ))
    for _ in range(spec.pos_on_track):
        po_seq += 1
        pos.append(_insert_po_with_operation(
            cur, code=f"{preset}-PO-{po_seq:04d}", sales_order_id=sales_order_id,
            status="IN_PROGRESS", due_date=(anchor + timedelta(days=14)).date(),
            created_at=anchor - timedelta(days=2), planned_quantity=180, done_qty=20, defect_qty=0, rework_qty=0,
        ))

    active_pos = pos  # empty for EMPTY_STATE -- session loops below simply no-op

    for i in range(spec.sessions_open_normal):
        if not active_pos:
            break
        employee_id = employee_ids[i % len(employee_ids)]
        target = active_pos[i % len(active_pos)]
        sid = _insert_session(
            cur, employee_id=employee_id, operation_id=target["operation_id"],
            station_id=station_ids[i % len(station_ids)], status="OPEN",
            started_at=anchor - timedelta(minutes=75), ended_at=None,
            good_qty=5 if not (spec.edge_cases and i == 0) else 0,
            defect_qty=0, rework_qty=0,
        )
        open_session_ids.append(sid)

    long_open_offset = spec.sessions_open_normal
    for i in range(spec.sessions_long_open):
        if not active_pos:
            break
        employee_id = employee_ids[(long_open_offset + i) % len(employee_ids)]
        target = active_pos[(long_open_offset + i) % len(active_pos)]
        sid = _insert_session(
            cur, employee_id=employee_id, operation_id=target["operation_id"],
            station_id=station_ids[i % len(station_ids)], status="OPEN",
            started_at=anchor - timedelta(hours=26), ended_at=None,
            good_qty=40, defect_qty=2, rework_qty=1,
        )
        open_session_ids.append(sid)
        _insert_exception(cur, session_id=sid, code="SESSION_LONG_OPEN",
                           note="Session open > 24h (seeded for UI Preview Lab)")

    for i in range(spec.sessions_closed_history):
        if not active_pos:
            break
        days_ago = 1 + (i * 29 // max(spec.sessions_closed_history, 1))
        started = anchor - timedelta(days=days_ago, hours=rng.choice([1, 3, 5, 7]))
        duration_hours = rng.choice([2, 3, 4, 6])
        ended = started + timedelta(hours=duration_hours)
        employee_id = employee_ids[i % len(employee_ids)]
        target = active_pos[i % len(active_pos)]
        defect = 3 if i % 5 == 0 else 0
        rework = 1 if defect and i % 10 == 0 else 0
        sid = _insert_session(
            cur, employee_id=employee_id, operation_id=target["operation_id"],
            station_id=station_ids[i % len(station_ids)], status="CLOSED",
            started_at=started, ended_at=ended, good_qty=30, defect_qty=defect, rework_qty=rework,
        )
        if defect >= 3:
            _insert_exception(cur, session_id=sid, code="HIGH_DEFECT_RATE",
                               note="Defect rate above threshold (seeded for UI Preview Lab)")

    return pos, open_session_ids
