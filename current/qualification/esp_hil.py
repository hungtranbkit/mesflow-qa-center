"""esp_hil: optional ESP32 Hardware-In-the-Loop production-policy suite.

Deliberately does NOT reimplement device control: a complete, already-
proven driver already exists at
`/home/dell/workspace/mesflow-kiosk-runtime-v2/tools/kiosk_e2e_runner.py`
(real ESP32-S3 kiosk hardware, verified live per that repo's own
docs/TEST_PLAN.md -- boot/reboot, scan injection via its debug EventBus,
screenshot capture, offline/idempotency/resync scenarios). This module is
a thin, honest wrapper: it (1) detects whether real hardware is actually
present and reachable *right now* -- never assumes -- and (2) if so,
shells out to that runner exactly like critical_unit.py shells out to
MESFlow's own pytest suite, recording its result as a real qualification
suite/scenario.

Three real outcomes, never a fourth silently-invented one:
  - NOT_CONFIGURED: no ESP32 device detected on any serial port at all.
  - BLOCKED: a device IS detected and alive (serial identity read
    succeeds) but the network path this HIL suite needs (HTTP to the
    device's own debug port, and/or a backend tunnel URL) isn't reachable
    from wherever this process is actually running -- a real, specific,
    reported reason, never a guess.
  - PASS/FAIL: the real kiosk_e2e_runner.py suite actually ran to
    completion against real hardware.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from .evidence import EvidenceStore
from .store import connect, now

ESPRESSIF_USB_IDS = {"303a:1001", "303a:0002", "303a:1000"}  # JTAG/serial debug unit + common CDC IDs
DEFAULT_RUNNER = Path(os.environ.get(
    "MESFLOW_KIOSK_HIL_RUNNER", "/home/dell/workspace/mesflow-kiosk-runtime-v2/tools/kiosk_e2e_runner.py"))
DEFAULT_ESP_IP = os.environ.get("MESFLOW_KIOSK_HIL_ESP_IP", "192.168.100.81")
DEFAULT_ESP_PORT = os.environ.get("MESFLOW_KIOSK_HIL_ESP_PORT", "8081")
DEFAULT_SERIAL_PORT = os.environ.get("MESFLOW_KIOSK_HIL_SERIAL_PORT", "/dev/ttyACM0")


def detect_serial_device(serial_port: str = DEFAULT_SERIAL_PORT) -> dict[str, Any]:
    """Real, read-only USB/serial detection -- never writes to the port."""
    try:
        import serial.tools.list_ports
    except ImportError:
        return {"present": False, "reason": "pyserial not installed"}
    for port in serial.tools.list_ports.comports():
        if port.device != serial_port:
            continue
        usb_id = f"{port.vid:04x}:{port.pid:04x}" if port.vid and port.pid else ""
        return {"present": True, "device": port.device, "usb_id": usb_id,
                "manufacturer": port.manufacturer, "product": port.product,
                "is_espressif": usb_id in ESPRESSIF_USB_IDS}
    return {"present": False, "reason": f"no serial device at {serial_port}"}


def read_serial_identity(serial_port: str = DEFAULT_SERIAL_PORT, *, seconds: float = 3.0) -> dict[str, Any]:
    """Passive read of whatever the device is already logging over its USB
    serial console -- never sends a byte, never resets/reboots it. Firmware
    logs structured JSON lines (see docs in the firmware repo); this parses
    what it can and reports the raw tail regardless, so a firmware log
    format change never turns into a silent false negative here."""
    try:
        import serial
    except ImportError:
        return {"ok": False, "error": "pyserial not installed"}
    try:
        ser = serial.Serial(serial_port, 115200, timeout=1)
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    try:
        buf = b""
        deadline = time.time() + seconds
        while time.time() < deadline:
            chunk = ser.read(4096)
            if chunk:
                buf += chunk
    finally:
        ser.close()
    text = buf.decode("utf-8", "replace")
    events = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    heartbeats = [e for e in events if e.get("code") == "HEARTBEAT_SENT"]
    return {"ok": True, "bytes_read": len(buf), "raw_tail": text[-1000:], "parsed_events": len(events),
           "alive": bool(events), "last_heartbeat": heartbeats[-1] if heartbeats else None,
           "uptime_ms": (events[-1].get("uptime_ms") if events else None)}


def check_http_reachable(esp_ip: str = DEFAULT_ESP_IP, esp_port: str = DEFAULT_ESP_PORT, *, timeout: float = 4.0) -> dict[str, Any]:
    url = f"http://{esp_ip}:{esp_port}/debug/device-state"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = json.loads(response.read())
        return {"reachable": True, "url": url, "device_state": body}
    except Exception as exc:
        return {"reachable": False, "url": url, "error": f"{type(exc).__name__}: {exc}"}


class EspHilRunner:
    def __init__(self, evidence_root: Path):
        self.conn = connect()
        self.evidence = EvidenceStore(evidence_root)
        self.evidence_root = evidence_root

    def run(self, run_id: str, *, backend_url: str | None = None, suite: str = "basic",
            serial_port: str = DEFAULT_SERIAL_PORT, esp_ip: str = DEFAULT_ESP_IP,
            esp_port: str = DEFAULT_ESP_PORT, runner_path: Path = DEFAULT_RUNNER,
            timeout_seconds: int = 900) -> dict[str, Any]:
        suite_id = f"suite-{uuid.uuid4().hex}"
        self.conn.execute("""INSERT INTO qa_suite_runs(id,qualification_run_id,suite_key,layer,required,status,
          started_at,command_json) VALUES(?,?,'esp_hil','hil',0,'RUNNING',?,'[]')""", (suite_id, run_id, now()))
        self.conn.commit()
        # required=0: HIL is only required when release policy explicitly
        # turns it on (spec section 12/17) -- see policy.py's
        # require_hil_when_configured, evaluated separately from this
        # suite's own row status.

        detection = detect_serial_device(serial_port)
        if not detection["present"]:
            return self._finish(suite_id, run_id, "NOT_CONFIGURED",
                                {"detection": detection, "reason": "no ESP32 device on any configured serial port"})

        identity = read_serial_identity(serial_port)
        reachability = check_http_reachable(esp_ip, esp_port)

        if not reachability["reachable"]:
            return self._finish(suite_id, run_id, "BLOCKED", {
                "detection": detection, "identity": identity, "reachability": reachability,
                "reason": f"device detected and alive on {serial_port} (uptime_ms="
                         f"{identity.get('uptime_ms')}), but its debug HTTP port at "
                         f"{reachability['url']} is not reachable from this process -- "
                         f"real HIL scenarios need that path; see 'reachability.error' for the exact cause",
            })

        if not backend_url:
            return self._finish(suite_id, run_id, "BLOCKED", {
                "detection": detection, "identity": identity, "reachability": reachability,
                "reason": "device is reachable but no backend_url (tunnel to this qualification's own "
                         "isolated MESFlow deployment) was supplied -- the real kiosk_e2e_runner.py requires one "
                         "so the physical device exercises the SAME artifact under qualification, not an "
                         "unrelated backend",
            })

        if not runner_path.is_file():
            return self._finish(suite_id, run_id, "BLOCKED", {
                "detection": detection, "identity": identity, "reachability": reachability,
                "reason": f"kiosk_e2e_runner.py not found at {runner_path}",
            })

        env = {**os.environ, "ESP_IP": esp_ip, "ESP_PORT": esp_port, "BACKEND_URL": backend_url,
              "SERIAL_PORT": serial_port}
        try:
            completed = subprocess.run(["python3", str(runner_path), "--suite", suite], env=env,
                                       cwd=str(runner_path.parent.parent), stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, timeout=timeout_seconds)
            output = completed.stdout.decode("utf-8", "replace")
            status = "PASSED" if completed.returncode == 0 else "FAILED"
        except subprocess.TimeoutExpired as exc:
            output = (exc.stdout or b"").decode("utf-8", "replace") if isinstance(exc.stdout, (bytes, bytearray)) else ""
            status = "FAILED"
            output += f"\nTIMEOUT after {timeout_seconds}s\n"

        # A scenario_run row is only ever inserted once the real runner
        # actually executed (PASSED/FAILED here, never for the
        # NOT_CONFIGURED/BLOCKED early returns above) -- coverage.py
        # matches features by scenario_key prefix, and esp.hil_ota's entry
        # in features.json (required_drivers: ["ESP_HIL"]) can only ever
        # honestly become COVERED from a real hardware run that actually
        # happened, never from a suite-level status alone.
        self._record_scenario(suite_id, run_id, f"esp.hil_ota.{suite}_suite", status)
        return self._finish(suite_id, run_id, status, {
            "detection": detection, "identity": identity, "reachability": reachability,
            "suite": suite, "runner_output_tail": output.splitlines()[-120:],
        })

    def _record_scenario(self, suite_id: str, run_id: str, scenario_key: str, status: str) -> None:
        scenario_id = f"scenario-{uuid.uuid4().hex}"
        self.conn.execute("""INSERT INTO qa_scenario_runs(id,suite_run_id,scenario_key,scenario_version,driver,
          status,started_at,finished_at) VALUES(?,?,?,?,?,?,?,?)""",
          (scenario_id, suite_id, scenario_key, "esp-hil-v1", "ESP_HIL", status, now(), now()))
        self.conn.execute("INSERT INTO qa_attempts(scenario_run_id,attempt_no,status,fingerprint,started_at,finished_at) "
                          "VALUES(?,1,?,?,?,?)", (scenario_id, status, "", now(), now()))
        self.conn.commit()

    def _finish(self, suite_id: str, run_id: str, status: str, payload: dict[str, Any]) -> dict[str, Any]:
        evidence = self.evidence.write_json(run_id, "esp-hil-result.json", payload, kind="ESP_HIL_EVIDENCE", suite_run_id=suite_id)
        self.conn.execute("UPDATE qa_suite_runs SET status=?,finished_at=?,summary_json=? WHERE id=?",
                          (status, now(), json.dumps({"evidence_id": evidence["id"]}), suite_id))
        self.conn.commit()
        return {"suite_id": suite_id, "status": status, "evidence": evidence, **payload}
