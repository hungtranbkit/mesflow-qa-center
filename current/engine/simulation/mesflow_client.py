"""Thin real-HTTP client used by every simulation actor.

Item 89 of the spec: "Do not call internal DB helpers to simulate normal
user behavior when an actual public/application API exists." Every method
here maps 1:1 to a real MESFlow endpoint a real kiosk browser tab or a real
logged-in web user would call -- no shortcuts into the database. The one
exception (fixtures for otherwise-impossible states) is intentionally NOT
in this file; see reconciliation.py's own docstring for that boundary.

A single `requests.Session` is reused per client instance (item 49: "Reuse
sessions like normal web users", not "authenticate on every API call").
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import requests


class MesflowApiError(Exception):
    """A MESFlow API call failed or returned ok=false. Carries enough
    detail for an incident record without needing the caller to re-parse
    the response."""

    def __init__(self, message: str, *, status: int | None = None, endpoint: str = "", body: Any = None):
        super().__init__(message)
        self.status = status
        self.endpoint = endpoint
        self.body = body


@dataclass
class MesflowClient:
    base_url: str
    timeout_s: float = 15.0
    session: requests.Session = field(default_factory=requests.Session)
    logged_in: bool = False

    def _url(self, path: str) -> str:
        return f"{self.base_url.rstrip('/')}{path}"

    def _request(self, method: str, path: str, *, json_body: dict | None = None,
                 params: dict | None = None, allow_status: tuple[int, ...] = ()) -> dict[str, Any]:
        url = self._url(path)
        try:
            resp = self.session.request(method, url, json=json_body, params=params, timeout=self.timeout_s)
        except requests.RequestException as exc:
            raise MesflowApiError(f"{method} {path} raised: {exc}", endpoint=path) from exc
        if resp.status_code >= 400 and resp.status_code not in allow_status:
            raise MesflowApiError(
                f"{method} {path} returned HTTP {resp.status_code}",
                status=resp.status_code, endpoint=path, body=resp.text[:2000],
            )
        try:
            data = resp.json()
        except ValueError:
            if resp.status_code in allow_status:
                return {"ok": False, "_http_status": resp.status_code, "_raw": resp.text[:500]}
            raise MesflowApiError(f"{method} {path} returned non-JSON body", status=resp.status_code, endpoint=path)
        data.setdefault("_http_status", resp.status_code)
        if data.get("ok") is False and resp.status_code not in allow_status:
            raise MesflowApiError(
                f"{method} {path} returned ok=false: {data.get('message') or data.get('error')}",
                status=resp.status_code, endpoint=path, body=data,
            )
        return data

    # --- Admin/web auth (item 49: login once, reuse session) --------------

    def login(self, username: str, password: str) -> None:
        self._request("POST", "/api/auth/login", json_body={"username": username, "password": password})
        self.logged_in = True

    # --- Admin bootstrap (factory_model.py) --------------------------------

    def create_employee(self, *, employee_no: str, name: str, department: str, team: str = "", position: str = "Công nhân") -> dict[str, Any]:
        return self._request("POST", "/api/employees", json_body={
            "employee_no": employee_no, "name": name, "department": department,
            "team": team, "position": position, "employment_status": "Đang làm", "active": True,
        })

    def create_user(self, *, username: str, display_name: str, role: str, password: str) -> dict[str, Any]:
        return self._request("POST", "/api/users", json_body={
            "username": username, "display_name": display_name, "role": role,
            "password": password, "must_change_password": False,
        }, allow_status=(409,))

    def create_station(self, *, code: str, name: str, workshop: str, production_line: str) -> dict[str, Any]:
        return self._request("POST", "/api/stations", json_body={
            "code": code, "name": name, "workshop": workshop, "production_line": production_line, "active": True,
        })

    def create_sales_order(self, *, code: str, customer_name: str) -> dict[str, Any]:
        return self._request("POST", "/api/sales-orders", json_body={
            "code": code, "customer_name": customer_name, "contract_no": "", "status": "CONFIRMED", "priority": "NORMAL",
        }, allow_status=(409,))

    def create_template(self, *, code: str, name: str) -> dict[str, Any]:
        return self._request("POST", "/api/templates", json_body={"code": code, "name": name, "product": "QA_SIMULATION"})

    def put_template_tree(self, template_id: int, tree: dict[str, Any]) -> dict[str, Any]:
        return self._request("PUT", f"/api/templates/{template_id}/tree", json_body=tree)

    def instantiate_template(self, template_id: int, *, sales_order_id: int, code: str, planned_quantity: int, due_in_days: int) -> dict[str, Any]:
        import datetime
        due = (datetime.date.today() + datetime.timedelta(days=due_in_days)).isoformat()
        return self._request("POST", f"/api/templates/{template_id}/instantiate", json_body={
            "sales_order_id": sales_order_id, "code": code, "planned_quantity": planned_quantity, "due_date": due,
        })

    def start_production_order(self, po_id: int) -> dict[str, Any]:
        return self._request("POST", f"/api/production-orders/{po_id}/start", allow_status=(409,))

    def list_resource(self, resource: str, *, limit: int = 500) -> list[dict[str, Any]]:
        data = self._request("GET", f"/api/{resource}", params={"limit": limit})
        return data.get("items") or []

    # --- Kiosk (public, no auth -- real kiosk tabs don't log in) ----------

    def kiosk_scan(self, qr: str) -> dict[str, Any]:
        return self._request("POST", "/api/kiosk-web/scan", json_body={"qr": qr}, allow_status=(400, 404, 409))

    def kiosk_start(self, *, employee_id: int, operation_id: int, station_id: int | None, device_uuid: str) -> dict[str, Any]:
        return self._request("POST", "/api/kiosk-web/start", json_body={
            "employee_id": employee_id, "operation_id": operation_id, "station_id": station_id,
            "device_uuid": device_uuid,
        }, allow_status=(400, 404, 409))

    def kiosk_finish(self, session_id: int, *, good_qty: int, defect_qty: int, rework_qty: int, note: str = "") -> dict[str, Any]:
        return self._request("POST", f"/api/kiosk-web/finish/{session_id}", json_body={
            "good_qty": good_qty, "defect_qty": defect_qty, "rework_qty": rework_qty, "note": note,
        }, allow_status=(400, 404, 409))

    def kiosk_heartbeat(self, device_uuid: str, status: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/api/kiosk-web/heartbeat", json_body={"device_uuid": device_uuid, **status})

    # --- Web reads (supervisor/manager actors) -----------------------------

    def dashboard_shift(self, *, shift_date: str, shift_id: int) -> dict[str, Any]:
        return self._request("GET", "/api/dashboard/shift", params={"shift_date": shift_date, "shift_id": shift_id, "limit": 1000})

    def employee_productivity(self) -> dict[str, Any]:
        return self._request("GET", "/api/reports/employee-productivity")

    def production_orders(self) -> dict[str, Any]:
        return self._request("GET", "/api/production-orders", params={"limit": 500})

    def exceptions(self) -> dict[str, Any]:
        return self._request("GET", "/api/session-exceptions", params={"limit": 200}, )
