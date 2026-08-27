"""One-time realistic factory bootstrap (items 44-47), built entirely
through the real admin API (item 89) -- never a direct DB insert. Returns
plain dicts (ids + realistic metadata) the run_manager persists into
sim_actors / sim_runs so a restart never re-creates the factory from
scratch (item 33)."""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

from .mesflow_client import MesflowClient

_GIVEN_NAMES = ("Văn An", "Thị Bình", "Văn Cường", "Thị Dung", "Văn Em", "Thị Giang", "Văn Hùng",
                "Thị Yến", "Văn Khoa", "Thị Lan", "Văn Minh", "Thị Nga", "Văn Oánh", "Thị Phương",
                "Văn Quang", "Thị Sương", "Văn Tâm", "Thị Uyên", "Văn Vinh", "Thị Xuân", "Văn Ý",
                "Thị Ánh", "Văn Bảo", "Thị Cẩm", "Văn Đạt", "Thị Hoa", "Văn Long", "Thị Mai",
                "Văn Nam", "Thị Oanh", "Văn Phúc", "Thị Quỳnh", "Văn Sơn", "Thị Thảo", "Văn Tuấn")
_SURNAMES = ("Nguyễn", "Trần", "Lê", "Phạm", "Hoàng", "Vũ", "Đặng", "Bùi", "Đỗ", "Ngô",
             "Dương", "Lý", "Phan", "Tôn", "Đinh", "Mai", "Trương", "Võ", "Lâm", "Châu",
             "Huỳnh", "Cao", "Lưu")

_DEPARTMENTS = ("Cắt laser", "Chấn", "Hàn", "Hoàn thiện", "Lắp ráp", "QC")

# Operation-type catalog, same realism pass as REPORT_30_DAYS (name, std
# seconds/unit range, realistic units/session range) -- kept independent
# (not imported from engine.preview.seed) so this package has no coupling
# to the UI Preview Lab's own generator, per item 79's module boundaries.
_OPERATION_TYPES: tuple[tuple[str, int, int, int, int], ...] = (
    ("Cắt laser", 35, 55, 20, 120),
    ("Dập", 20, 40, 50, 300),
    ("Chấn CNC", 60, 90, 15, 100),
    ("Hàn MIG", 140, 220, 5, 40),
    ("Mài", 70, 110, 10, 60),
    ("Lắp ráp", 90, 160, 5, 50),
    ("Kiểm tra QC", 45, 75, 20, 150),
    ("Hoàn thiện", 90, 140, 10, 80),
)


@dataclass
class FactoryEmployee:
    external_id: int
    employee_no: str
    name: str
    department: str
    qr: str


@dataclass
class FactoryOperation:
    operation_id: int
    code: str
    name: str
    qr: str
    standard_seconds_per_unit: float
    qty_range: tuple[int, int]
    po_id: int
    po_code: str


@dataclass
class WebUserAccount:
    username: str
    display_name: str
    role: str
    password: str


@dataclass
class FactoryModel:
    run_tag: str
    sales_order_id: int
    employees: list[FactoryEmployee] = field(default_factory=list)
    station_ids: list[int] = field(default_factory=list)
    operations: list[FactoryOperation] = field(default_factory=list)
    po_ids: list[int] = field(default_factory=list)
    web_users: list[WebUserAccount] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_tag": self.run_tag,
            "sales_order_id": self.sales_order_id,
            "employees": [e.__dict__ for e in self.employees],
            "station_ids": self.station_ids,
            "operations": [o.__dict__ for o in self.operations],
            "po_ids": self.po_ids,
            "web_users": [u.__dict__ for u in self.web_users],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "FactoryModel":
        return cls(
            run_tag=d["run_tag"], sales_order_id=d["sales_order_id"],
            employees=[FactoryEmployee(**e) for e in d.get("employees", [])],
            station_ids=list(d.get("station_ids", [])),
            operations=[FactoryOperation(**o) for o in d.get("operations", [])],
            po_ids=list(d.get("po_ids", [])),
            web_users=[WebUserAccount(**u) for u in d.get("web_users", [])],
        )


def _random_name(rng: random.Random) -> str:
    return f"{rng.choice(_SURNAMES)} {rng.choice(_GIVEN_NAMES)}"


def _template_tree(rng: random.Random, size: str, op_cursor: list[int]) -> dict[str, Any]:
    """Item 45: simple (1 part/3 ops), medium (3 parts/10 ops), complex
    (6-10 parts/30+ ops). Some parts get a linear input_flow_enabled chain
    (item 9/45's "some with linear material flow"), most don't (item 79's
    Phase A scope note: WIP-constraint enforcement itself is Phase C)."""
    n_parts = {"simple": 1, "medium": 3, "complex": rng.randint(6, 10)}[size]
    ops_per_part = {"simple": 3, "medium": 4, "complex": 4}[size]
    parts, operations = [], []
    for p in range(n_parts):
        part_key = f"P{p+1}"
        parts.append({"key": part_key, "code": part_key, "name": f"Chi tiết {part_key}"})
        prev_code = None
        for o in range(ops_per_part):
            op_type, lo, hi, qlo, qhi = _OPERATION_TYPES[op_cursor[0] % len(_OPERATION_TYPES)]
            op_cursor[0] += 1
            code = f"{part_key}-OP{o+1}"
            std = float(rng.randint(lo, hi))
            entry = {"part_key": part_key, "code": code, "name": op_type,
                     "standard_seconds_per_unit": std, "input_flow_enabled": False}
            # Linear chain within a part, ~50% of the time (item 9/45).
            if prev_code and rng.random() < 0.5:
                entry["input_flow_enabled"] = True
                entry["input_source_code"] = prev_code
                entry["input_source_kind"] = "GOOD"
            operations.append(entry)
            prev_code = code
    return {"parts": parts, "operations": operations, "equipment": []}


def bootstrap_factory(client: MesflowClient, *, run_tag: str, profile_counts: dict[str, int],
                       seed: int) -> FactoryModel:
    """Creates employees/stations/templates/POs via the real admin API and
    starts the POs (item 10: RELEASED/IN_PROGRESS is where normal
    distribution should heavily favor production). `run_tag` (e.g.
    "SIM-20260823-ab12") prefixes every created code so simulation-owned
    entities are always identifiable (item 42) and multiple runs never
    collide."""
    rng = random.Random(seed)
    model = FactoryModel(run_tag=run_tag, sales_order_id=0)

    so = client.create_sales_order(code=f"QA-SO-{run_tag}", customer_name="QA Simulation Factory")
    model.sales_order_id = so["id"]

    n_employees = profile_counts.get("employees", 15)
    used_names: set[str] = set()
    for i in range(n_employees):
        name = _random_name(rng)
        while name in used_names:
            name = _random_name(rng)
        used_names.add(name)
        employee_no = f"QA-E{i+1:03d}-{run_tag}"
        dept = _DEPARTMENTS[i % len(_DEPARTMENTS)]
        created = client.create_employee(employee_no=employee_no, name=name, department=dept, team=f"Ca {1 + i % 2}")
        item = created.get("item") or {}
        model.employees.append(FactoryEmployee(
            external_id=created["id"], employee_no=employee_no, name=name, department=dept,
            qr=item.get("qr") or f"WF|EMP|{employee_no}",
        ))

    for kind, role, count in (("SV", "supervisor", profile_counts.get("supervisors", 2)),
                              ("MG", "manager", profile_counts.get("managers", 1))):
        for i in range(count):
            username = f"qa-{role}-{run_tag}-{i+1}".lower()
            password = f"QaSim!{run_tag}{i+1}"
            client.create_user(username=username, display_name=f"QA {role.title()} {i+1}", role=role, password=password)
            model.web_users.append(WebUserAccount(username=username, display_name=role, role=role, password=password))

    n_kiosks = max(1, profile_counts.get("kiosks", 2))
    for i in range(n_kiosks):
        st = client.create_station(code=f"QA-ST-{i+1:02d}-{run_tag}", name=f"Trạm {i+1:02d}",
                                    workshop="Xưởng QA Sim", production_line="Line SIM")
        model.station_ids.append(st["id"])

    # Item 45: product mix -- a handful of simple templates, a couple
    # medium, at most one complex, matching realistic PO counts (item 71)
    # rather than one giant template.
    n_active_pos = max(1, profile_counts.get("active_pos", 3))
    sizes = (["simple"] * max(1, n_active_pos // 2) + ["medium"] * max(1, n_active_pos // 3)
              + ["complex"] * max(0, n_active_pos - n_active_pos // 2 - n_active_pos // 3))
    op_cursor = [0]
    for i, size in enumerate(sizes[:n_active_pos]):
        tmpl_code = f"QA-TMPL-{size.upper()}-{run_tag}-{i+1}"
        tmpl = client.create_template(code=tmpl_code, name=tmpl_code)
        template_id = tmpl["id"]
        tree = _template_tree(rng, size, op_cursor)
        client.put_template_tree(template_id, tree)
        planned_qty = {"small": (10, 50), "medium": (50, 500), "large": (500, 5000)}[
            rng.choices(["small", "medium", "large"], weights=[0.5, 0.4, 0.1], k=1)[0]
        ]
        qty = rng.randint(*planned_qty)
        po_code = f"QA-PO-{run_tag}-{i+1:03d}"
        result = client.instantiate_template(template_id, sales_order_id=model.sales_order_id,
                                              code=po_code, planned_quantity=qty, due_in_days=rng.randint(7, 45))
        po_id = result["production_order_id"]
        model.po_ids.append(po_id)
        client.start_production_order(po_id)

        for op in client.list_resource("operations", limit=200):
            if op.get("production_order_id") != po_id:
                continue
            op_type_row = next((t for t in _OPERATION_TYPES if t[0] == op.get("name")), None)
            qty_range = (op_type_row[3], op_type_row[4]) if op_type_row else (5, 100)
            model.operations.append(FactoryOperation(
                operation_id=op["id"], code=op["code"], name=op["name"], qr=op.get("qr") or f"WF|OP|{op['code']}",
                standard_seconds_per_unit=float(op.get("standard_seconds_per_unit") or 60.0),
                qty_range=qty_range, po_id=po_id, po_code=po_code,
            ))

    return model
