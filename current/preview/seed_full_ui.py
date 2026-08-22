from __future__ import annotations

import json
import os
import random
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg import sql

PRESET = os.environ.get("UI_PREVIEW_PRESET", "FULL_UI").strip().upper()
if os.environ.get("MESFLOW_UI_PREVIEW") != "1":
    raise SystemExit("REFUSE: MESFLOW_UI_PREVIEW=1 is required")
DATABASE_URL = os.environ["DATABASE_URL"]
NOW = datetime.now(timezone.utc)
RNG = random.Random(20260822)

DOMAIN_TABLES = [
    "operation_adjustments", "qc_inspections", "penalty_tickets", "work_sessions",
    "kiosk_status", "kiosk_identities", "operations", "parts", "production_orders",
    "sales_orders", "equipment", "stations", "employees", "template_operations",
    "template_parts", "template_equipment", "templates",
]


def exists(cur, table):
    return bool(cur.execute("SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name=%s", (table,)).fetchone())


def cols(cur, table):
    return {r[0] for r in cur.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=%s", (table,)).fetchall()}


def ins(cur, table, values, returning="id"):
    available = cols(cur, table)
    data = {k: v for k, v in values.items() if k in available}
    q = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
        sql.Identifier(table),
        sql.SQL(",").join(map(sql.Identifier, data)),
        sql.SQL(",").join(sql.Placeholder() for _ in data),
    )
    if returning in available:
        q += sql.SQL(" RETURNING {}").format(sql.Identifier(returning))
        return cur.execute(q, tuple(data.values())).fetchone()[0]
    cur.execute(q, tuple(data.values()))
    return None


def config(name):
    return {
        "EMPTY_STATE": (0, 0, 0),
        "NORMAL_FACTORY": (10, 6, 14),
        "PROBLEM_FACTORY": (14, 10, 30),
        "REPORT_30_DAYS": (20, 14, 30),
        "EDGE_CASES": (8, 7, 7),
        "FULL_UI": (14, 10, 30),
    }.get(name, (14, 10, 30))


def main():
    employee_count, po_count, history_days = config(PRESET)
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            db = cur.execute("SELECT current_database()").fetchone()[0]
            if not str(db).startswith("mesflow_ui_"):
                raise RuntimeError(f"REFUSE_NON_PREVIEW_DATABASE:{db}")

            target = [t for t in DOMAIN_TABLES if exists(cur, t)]
            if target:
                cur.execute(sql.SQL("TRUNCATE {} RESTART IDENTITY CASCADE").format(sql.SQL(",").join(map(sql.Identifier, target))))

            if PRESET == "EMPTY_STATE":
                conn.commit()
                print(json.dumps({"ok": True, "preset": PRESET, "database": db, "counts": {}}, ensure_ascii=False))
                return

            names = ["Nguyễn Minh An", "Trần Quốc Bảo", "Lê Hoàng Duy", "Phạm Gia Huy", "Võ Thanh Long", "Đặng Ngọc Mai", "Bùi Tuấn Nam", "Hoàng Khánh Phương", "Đỗ Minh Quân", "Nguyễn Thu Trang", "Trần Anh Vũ", "Lê Hải Yến", "Phạm Đức Thành", "Võ Ngọc Linh", "Bùi Nhật Minh", "Đặng Thảo Vy", "Hoàng Gia Bảo", "Đỗ Quỳnh Anh", "Nguyễn Thành Đạt", "Trần Minh Châu"]
            departments = ["Cắt laser", "Chấn", "Hàn", "Lắp ráp", "QC"]
            employees = []
            for i in range(employee_count):
                no = f"UIPREVIEW-EMP-{i+1:03d}"
                name = names[i % len(names)]
                if PRESET == "EDGE_CASES" and i == 0:
                    name = "Nhân viên kiểm thử tên rất dài — Nguyễn Văn Example With Extremely Long Display Name"
                employees.append(ins(cur, "employees", {
                    "employee_no": no, "name": name, "department": departments[i % len(departments)],
                    "team": f"Tổ {(i % 4)+1}", "position": "Tổ trưởng" if i % 5 == 0 else "Công nhân",
                    "employment_status": "Đang làm", "active": True, "qr": f"WF|EMP|{no}",
                    "start_date": (NOW - timedelta(days=400+i*7)).date(),
                    "created_at": NOW - timedelta(days=500), "updated_at": NOW - timedelta(days=i),
                }))

            stations = []
            for i, name in enumerate(["Laser 01", "Chấn 01", "Hàn 01", "Lắp ráp 01"], 1):
                stations.append(ins(cur, "stations", {"code": f"UIPREVIEW-ST-{i:02d}", "name": name, "workshop": "Xưởng chính", "production_line": f"Line {i}", "active": True}))

            equipment = []
            for i, (name, kind) in enumerate([("Máy cắt Fiber Laser", "LASER"), ("Máy chấn CNC", "BENDING"), ("Máy hàn MIG", "WELDING"), ("Bàn lắp ráp", "ASSEMBLY"), ("Máy mài", "GRINDING"), ("Thiết bị QC", "QC")], 1):
                equipment.append(ins(cur, "equipment", {"code": f"UIPREVIEW-EQ-{i:02d}", "name": name, "equipment_type": kind, "status": "MAINTENANCE" if PRESET == "PROBLEM_FACTORY" and i == 5 else "ACTIVE", "active": True, "notes": "UI Preview Lab"}))

            sales = []
            for i in range(max(2, (po_count + 2)//3)):
                sales.append(ins(cur, "sales_orders", {"code": f"UIPREVIEW-SO-{i+1:03d}", "customer_name": ["KIMEX Demo", "Công ty An Phát", "Global Manufacturing", "Khách hàng nội bộ"][i % 4], "contract_no": f"HD-2026-{i+1:03d}", "status": "CONFIRMED", "priority": ["NORMAL", "HIGH", "URGENT"][i % 3], "delivery_deadline": (NOW + timedelta(days=5+i*3)).date(), "notes": "Generated by UI Preview Lab"}))

            statuses = ["DRAFT", "PLANNED", "RELEASED", "IN_PROGRESS", "PAUSED", "COMPLETED", "IN_PROGRESS", "COMPLETED", "IN_PROGRESS", "PLANNED"]
            due = [-9, -3, 0, 1, 4, -1, 7, -6, 2, 14]
            products = ["Khung máy A", "Tủ điện B", "Giá đỡ C", "Cụm băng tải D", "Vỏ máy E"]
            operations = []
            po_ids = []
            for i in range(po_count):
                status = statuses[i % len(statuses)]
                planned = [80, 120, 250, 500, 1000, 60][i % 6]
                if PRESET == "EDGE_CASES" and i == 0:
                    planned = 999999
                code = f"UIPREVIEW-PO-{i+1:03d}"
                product = products[i % len(products)]
                if PRESET == "EDGE_CASES" and i == 1:
                    product += " — Tên sản phẩm rất dài để kiểm tra responsive layout và overflow"
                po = ins(cur, "production_orders", {"code": code, "sales_order_id": sales[i % len(sales)], "product": product, "planned_quantity": planned, "status": status, "priority": ["LOW", "NORMAL", "HIGH", "URGENT"][i % 4], "due_date": (NOW + timedelta(days=due[i % len(due)])).date(), "planned_start_at": NOW - timedelta(days=max(0, 8-i)), "planned_end_at": NOW + timedelta(days=due[i % len(due)]), "notes": "UI Preview: dữ liệu có chủ đích để cover dashboard/report/exception.", "created_at": NOW - timedelta(days=35-min(i,20)), "updated_at": NOW - timedelta(hours=i)})
                po_ids.append(po)
                for part_no in range(2):
                    part = ins(cur, "parts", {"production_order_id": po, "code": f"P{part_no+1}", "name": ["Thân chính", "Nắp / gá phụ"][part_no], "drawing_path": "", "sort_order": part_no+1, "active": True})
                    for op_no, op_name in enumerate(["Cắt laser", "Chấn CNC", "Hàn hoàn thiện"], 1):
                        progress = 1.0 if status == "COMPLETED" else (0.35 + 0.1*((i+op_no)%4) if status == "IN_PROGRESS" else (0.22 if status == "PAUSED" else 0.0))
                        done = int(planned * min(progress, 1.0))
                        defect = 0 if progress == 0 else max(0, int(done*(0.01+0.01*(op_no%2))))
                        op_status = "COMPLETED" if status == "COMPLETED" else ("IN_PROGRESS" if status in {"IN_PROGRESS", "PAUSED"} else status)
                        op = ins(cur, "operations", {"production_order_id": po, "part_id": part, "equipment_id": equipment[(op_no-1) % len(equipment)], "code": f"{code}-P{part_no+1}-OP{op_no}", "name": op_name, "plan_qty": planned, "done_qty": done, "defect_qty": defect, "rework_qty": defect//3, "status": op_status, "sort_order": op_no, "qr": f"WF|OP|{code}-P{part_no+1}-OP{op_no}", "standard_seconds_per_unit": [42,75,150][op_no-1], "repair_cycle_time_seconds_per_unit": [30,60,120][op_no-1], "planned_start_at": NOW-timedelta(days=4-min(i,3),hours=op_no), "planned_end_at": NOW+timedelta(days=max(-2,due[i % len(due)]),hours=op_no), "created_at": NOW-timedelta(days=30), "updated_at": NOW-timedelta(hours=op_no)})
                        operations.append((op, status, op_no))

            session_count = 0
            eligible = [x for x in operations if x[1] in {"IN_PROGRESS", "PAUSED", "COMPLETED"}]
            if employees and eligible:
                for day_back in range(history_days, -1, -1):
                    base = NOW - timedelta(days=day_back)
                    for j in range(max(2, min(len(employees), 4 + day_back % 7))):
                        emp = employees[(j+day_back) % len(employees)]
                        op, _, op_no = eligible[(j*3+day_back) % len(eligible)]
                        start = base.replace(hour=1+(j%8), minute=(j*11)%60, second=0, microsecond=0)
                        duration = 45 + ((j*29+day_back*7) % 180)
                        end = start + timedelta(minutes=duration)
                        defect = 1 if (j+day_back) % 9 == 0 else 0
                        ins(cur, "work_sessions", {"employee_id": emp, "operation_id": op, "station_id": stations[(j+op_no) % len(stations)], "device_uuid": f"UIPREVIEW-KIOSK-{(j%4)+1:02d}", "status": "CLOSED", "started_at": start, "ended_at": end, "good_qty": max(1, int(duration*60/[42,75,150][op_no-1])), "defect_qty": defect, "rework_qty": 1 if defect and (j+day_back)%3 == 0 else 0, "note": "UI Preview normal session" if not defect else "UI Preview có lỗi sản phẩm", "start_request_id": f"UIPREVIEW-START-{day_back}-{j}-{op}", "finish_request_id": f"UIPREVIEW-FINISH-{day_back}-{j}-{op}", "created_at": start, "updated_at": end})
                        session_count += 1

                active = [x for x in operations if x[1] in {"IN_PROGRESS", "PAUSED"}]
                for j, (op, _, _) in enumerate(active[:min(5, len(employees))]):
                    start = NOW - timedelta(minutes=35+j*28)
                    if PRESET in {"FULL_UI", "PROBLEM_FACTORY", "EDGE_CASES"} and j == 0:
                        start = NOW - timedelta(hours=54 if PRESET == "EDGE_CASES" else 26)
                    ins(cur, "work_sessions", {"employee_id": employees[j], "operation_id": op, "station_id": stations[j % len(stations)], "device_uuid": f"UIPREVIEW-KIOSK-{j+1:02d}", "status": "OPEN", "started_at": start, "ended_at": None, "good_qty": 0, "defect_qty": 0, "rework_qty": 0, "note": "Session mở lâu có chủ đích để hiện exception" if j == 0 else "Session đang chạy", "start_request_id": f"UIPREVIEW-OPEN-{j}-{op}", "finish_request_id": None, "created_at": start, "updated_at": NOW-timedelta(minutes=3)})
                    session_count += 1

            if exists(cur, "kiosk_identities"):
                for i, station in enumerate(stations, 1):
                    ins(cur, "kiosk_identities", {"device_uuid": f"UIPREVIEW-KIOSK-{i:02d}", "device_name": f"Kiosk Preview {i}", "station_id": station, "status": "APPROVED", "token_hash": "", "firmware_version": ["5.3.3","5.3.2","5.2.9","5.3.3"][i-1], "last_ip": f"10.99.0.{20+i}", "last_seen_at": NOW-timedelta(minutes=i*3)})
            if exists(cur, "kiosk_status"):
                for i, station in enumerate(stations, 1):
                    ins(cur, "kiosk_status", {"device_uuid": f"UIPREVIEW-KIOSK-{i:02d}", "station_id": station, "ui_state": ["IDLE","WORKING","WAIT_QTY","ERROR"][i-1], "health_state": "HEALTHY" if i < 4 else "WARNING", "queue_size": 0 if i < 4 else 7, "wifi_rssi": [-48,-55,-64,-82][i-1], "free_heap": 190000-i*12000, "last_error": "" if i < 4 else "Preview: mất kết nối tạm thời", "last_heartbeat_at": NOW-timedelta(minutes=i*3), "updated_at": NOW-timedelta(minutes=i*3)}, returning="device_uuid")

            conn.commit()
            print(json.dumps({"ok": True, "preset": PRESET, "database": db, "time_anchor": NOW.isoformat(), "counts": {"employees": len(employees), "production_orders": len(po_ids), "operations": len(operations), "sessions": session_count, "history_days": history_days}}, ensure_ascii=False))

if __name__ == "__main__":
    main()
