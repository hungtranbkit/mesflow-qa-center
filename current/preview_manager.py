from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import jsonify, render_template, request


PREVIEW_LABEL = "com.mesflow.qa.preview"
PREVIEW_ID_LABEL = "com.mesflow.qa.preview.id"
IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@:-]{0,250}$")
PRESETS = ("FULL_UI", "NORMAL_FACTORY", "PROBLEM_FACTORY", "REPORT_30_DAYS", "EMPTY_STATE", "EDGE_CASES")


class PreviewError(RuntimeError):
    pass


class PreviewManager:
    def __init__(self, root: Path):
        self.root = Path(root)
        state_dir = Path(os.environ.get("MESFLOW_QA_STATE_DIR", str(self.root / "runtime")))
        state_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = state_dir / "ui_preview.json"
        self.seed_script = self.root / "preview" / "seed_full_ui.py"
        self._lock = threading.RLock()
        self._worker: threading.Thread | None = None

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _write(self, data: dict[str, Any]) -> dict[str, Any]:
        data = dict(data)
        data["updated_at"] = datetime.now().isoformat(timespec="seconds")
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.state_file)
        return data

    def _docker(self, *args: str, input_text: str | None = None, timeout: int = 60, check: bool = True) -> subprocess.CompletedProcess:
        if not shutil_which("docker"):
            raise PreviewError("DOCKER_CLI_NOT_FOUND")
        proc = subprocess.run(
            ["docker", *args],
            input=input_text,
            text=True,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        if check and proc.returncode != 0:
            msg = (proc.stderr or proc.stdout or "").strip()
            raise PreviewError(f"docker {' '.join(args[:4])}: {msg[-1200:]}")
        return proc

    def capability(self) -> dict[str, Any]:
        docker_ok = bool(shutil_which("docker"))
        socket_ok = Path("/var/run/docker.sock").exists()
        seed_ok = self.seed_script.is_file()
        return {"docker_cli": docker_ok, "docker_socket": socket_ok, "seed_script": seed_ok, "ready": docker_ok and socket_ok and seed_ok}

    def _used_ports(self) -> set[int]:
        result = self._docker("ps", "-a", "--format", "{{.Ports}}", check=False)
        used: set[int] = set()
        for match in re.finditer(r"(?:127\.0\.0\.1|0\.0\.0\.0|\[::\]):(\d+)->", result.stdout or ""):
            used.add(int(match.group(1)))
        return used

    def _choose_port(self) -> int:
        start = int(os.environ.get("MESFLOW_PREVIEW_PORT_START", "18080"))
        used = self._used_ports()
        for port in range(start, start + 70):
            if port not in used:
                return port
        raise PreviewError("NO_FREE_PREVIEW_PORT")

    def _inspect_label(self, resource_type: str, name: str, label: str) -> str:
        if not name:
            return ""
        if resource_type == "network":
            args = ("network", "inspect", "-f", f'{{{{ index .Labels "{label}" }}}}', name)
        else:
            args = ("inspect", "-f", f'{{{{ index .Config.Labels "{label}" }}}}', name)
        r = self._docker(*args, check=False)
        return (r.stdout or "").strip() if r.returncode == 0 else ""

    def _assert_owned_container(self, name: str) -> None:
        if self._inspect_label("container", name, PREVIEW_LABEL) != "1":
            raise PreviewError(f"REFUSE_NON_PREVIEW_CONTAINER:{name}")

    def _assert_owned_network(self, name: str) -> None:
        if self._inspect_label("network", name, PREVIEW_LABEL) != "1":
            raise PreviewError(f"REFUSE_NON_PREVIEW_NETWORK:{name}")

    def _container_exists(self, name: str) -> bool:
        return self._docker("inspect", name, check=False).returncode == 0

    def _container_running(self, name: str) -> bool:
        r = self._docker("inspect", "-f", "{{.State.Running}}", name, check=False)
        return r.returncode == 0 and (r.stdout or "").strip().lower() == "true"

    def _container_status(self, name: str) -> str:
        r = self._docker("inspect", "-f", "{{.State.Status}}", name, check=False)
        return (r.stdout or "").strip() if r.returncode == 0 else "missing"

    def status(self) -> dict[str, Any]:
        state = self._read()
        cap = self.capability()
        if not state:
            return {"phase": "IDLE", "capability": cap, "presets": PRESETS, "default_image": self.default_image()}
        app_name = str(state.get("app_container") or "")
        db_name = str(state.get("db_container") or "")
        state["app_runtime"] = self._container_status(app_name) if app_name else "missing"
        state["db_runtime"] = self._container_status(db_name) if db_name else "missing"
        state["capability"] = cap
        state["presets"] = PRESETS
        state["default_image"] = self.default_image()
        return state

    def default_image(self) -> str:
        return os.environ.get("MESFLOW_PREVIEW_IMAGE", "mesflow-app:71.0.0.46").strip()

    def start_async(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            current = self._read()
            if current.get("preview_id"):
                raise PreviewError("PREVIEW_ALREADY_EXISTS_DELETE_FIRST")
            if self._worker and self._worker.is_alive():
                raise PreviewError("PREVIEW_JOB_ALREADY_RUNNING")

            preset = str(payload.get("preset") or "FULL_UI").strip().upper()
            if preset not in PRESETS:
                raise PreviewError("INVALID_PRESET")
            image = str(payload.get("image") or self.default_image()).strip()
            if not IMAGE_RE.fullmatch(image):
                raise PreviewError("INVALID_MESFLOW_IMAGE")

            preview_id = datetime.now().strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:5]
            suffix = re.sub(r"[^a-z0-9]", "", preview_id.lower())[-12:]
            state = {
                "phase": "CREATING",
                "preview_id": preview_id,
                "preset": preset,
                "image": image,
                "port": self._choose_port(),
                "db_name": f"mesflow_ui_{suffix}",
                "db_user": "mesflow_ui",
                "db_password": secrets.token_hex(18),
                "db_container": f"mesflow-ui-db-{suffix}",
                "app_container": f"mesflow-ui-preview-{suffix}",
                "network": f"mesflow-ui-net-{suffix}",
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "message": "Đang tạo PostgreSQL preview riêng",
            }
            self._write(state)
            self._worker = threading.Thread(target=self._create_worker, args=(state,), daemon=True)
            self._worker.start()
            return self.public_state(state)

    def public_state(self, state: dict[str, Any]) -> dict[str, Any]:
        result = dict(state)
        result.pop("db_password", None)
        result.pop("preview_password", None)
        return result

    def _create_worker(self, state: dict[str, Any]) -> None:
        try:
            cap = self.capability()
            if not cap["ready"]:
                raise PreviewError(f"PREVIEW_RUNTIME_NOT_READY:{cap}")

            labels = ["--label", f"{PREVIEW_LABEL}=1", "--label", f"{PREVIEW_ID_LABEL}={state['preview_id']}"]
            self._docker("network", "create", *labels, state["network"], timeout=30)

            postgres_image = os.environ.get("MESFLOW_PREVIEW_POSTGRES_IMAGE", "postgres:17-alpine")
            self._docker(
                "run", "-d", "--name", state["db_container"],
                "--network", state["network"], *labels,
                "--tmpfs", "/var/lib/postgresql/data:rw,size=1g",
                "-e", f"POSTGRES_DB={state['db_name']}",
                "-e", f"POSTGRES_USER={state['db_user']}",
                "-e", f"POSTGRES_PASSWORD={state['db_password']}",
                "-e", "TZ=Asia/Ho_Chi_Minh",
                postgres_image,
                timeout=90,
            )
            self._wait_postgres(state)
            state["message"] = "PostgreSQL riêng đã sẵn sàng; đang khởi động MESFlow image mới"
            self._write(state)

            db_url = f"postgresql://{state['db_user']}:{state['db_password']}@{state['db_container']}:5432/{state['db_name']}"
            preview_admin_password = "Preview12345!"
            self._docker(
                "run", "-d", "--name", state["app_container"],
                "--network", state["network"], *labels,
                "-p", f"127.0.0.1:{state['port']}:8080",
                "-e", f"DATABASE_URL={db_url}",
                "-e", f"WORKSHOP_DATABASE_URL={db_url}",
                "-e", "MESFLOW_ENV=preview",
                "-e", "MESFLOW_UI_PREVIEW=1",
                "-e", "MESFLOW_TIMEZONE=Asia/Ho_Chi_Minh",
                "-e", "MESFLOW_ADMIN_USERNAME=admin",
                "-e", f"MESFLOW_ADMIN_PASSWORD={preview_admin_password}",
                "-e", "MESFLOW_TEST_AUTO_LOGIN=1",
                "-e", "MESFLOW_TEST_AUTO_LOGIN_USERNAME=admin",
                "-e", "MESFLOW_LOCAL_AUTO_LOGIN=1",
                "-e", "MESFLOW_INTERNAL_HTTP_SESSION=1",
                state["image"],
                timeout=120,
            )
            self._wait_mesflow(state)
            state["phase"] = "SEEDING"
            state["message"] = f"MESFlow đã migrate schema; đang seed {state['preset']}"
            self._write(state)
            seed_result = self._seed(state, state["preset"])
            state["phase"] = "READY"
            state["message"] = "Preview sẵn sàng"
            state["seed_result"] = seed_result
            state["preview_admin"] = "admin"
            state["preview_password"] = preview_admin_password
            self._write(state)
        except Exception as exc:
            state["phase"] = "ERROR"
            state["message"] = f"{type(exc).__name__}: {exc}"
            try:
                if state.get("app_container") and self._container_exists(state["app_container"]):
                    logs = self._docker("logs", "--tail", "120", state["app_container"], check=False).stdout
                    state["last_logs"] = (logs or "")[-12000:]
            except Exception:
                pass
            self._write(state)

    def _wait_postgres(self, state: dict[str, Any], timeout: int = 90) -> None:
        deadline = time.monotonic() + timeout
        last = ""
        while time.monotonic() < deadline:
            r = self._docker(
                "exec", state["db_container"], "pg_isready",
                "-U", state["db_user"], "-d", state["db_name"],
                check=False, timeout=10,
            )
            if r.returncode == 0:
                return
            last = (r.stderr or r.stdout or "").strip()
            time.sleep(1)
        raise PreviewError(f"POSTGRES_NOT_READY:{last}")

    def _wait_mesflow(self, state: dict[str, Any], timeout: int = 150) -> None:
        deadline = time.monotonic() + timeout
        last = ""
        while time.monotonic() < deadline:
            r = self._docker(
                "exec", state["app_container"], "curl", "-fsS",
                "http://127.0.0.1:8080/api/system/ready",
                check=False, timeout=10,
            )
            if r.returncode == 0:
                return
            last = (r.stderr or r.stdout or "").strip()
            if not self._container_running(state["app_container"]):
                logs = self._docker("logs", "--tail", "100", state["app_container"], check=False).stdout
                raise PreviewError(f"MESFLOW_CONTAINER_EXITED:{(logs or '')[-5000:]}")
            time.sleep(2)
        logs = self._docker("logs", "--tail", "100", state["app_container"], check=False).stdout
        raise PreviewError(f"MESFLOW_NOT_READY:{last}\n{(logs or '')[-5000:]}")

    def _seed(self, state: dict[str, Any], preset: str) -> dict[str, Any]:
        if not self.seed_script.is_file():
            raise PreviewError("SEED_SCRIPT_NOT_FOUND")
        self._assert_owned_container(state["app_container"])
        script = self.seed_script.read_text(encoding="utf-8")
        r = self._docker(
            "exec", "-i",
            "-e", "MESFLOW_UI_PREVIEW=1",
            "-e", f"UI_PREVIEW_PRESET={preset}",
            state["app_container"], "python", "-",
            input_text=script, timeout=120,
        )
        output = (r.stdout or "").strip()
        try:
            return json.loads(output.splitlines()[-1])
        except Exception:
            return {"ok": True, "output": output[-5000:]}

    def reseed(self, preset: str) -> dict[str, Any]:
        with self._lock:
            state = self._read()
            if state.get("phase") not in {"READY", "STOPPED"}:
                raise PreviewError("PREVIEW_NOT_READY")
            preset = str(preset or state.get("preset") or "FULL_UI").upper()
            if preset not in PRESETS:
                raise PreviewError("INVALID_PRESET")
            if state.get("phase") == "STOPPED":
                self.resume()
                state = self._read()
            state["phase"] = "SEEDING"
            state["message"] = f"Đang reset và seed lại {preset}"
            self._write(state)
            try:
                result = self._seed(state, preset)
                state["preset"] = preset
                state["seed_result"] = result
                state["phase"] = "READY"
                state["message"] = "Re-seed hoàn tất"
                self._write(state)
                return self.public_state(state)
            except Exception:
                state["phase"] = "ERROR"
                self._write(state)
                raise

    def stop(self) -> dict[str, Any]:
        with self._lock:
            state = self._read()
            if not state:
                raise PreviewError("NO_PREVIEW")
            for key in ("app_container", "db_container"):
                name = str(state.get(key) or "")
                if name and self._container_exists(name):
                    self._assert_owned_container(name)
                    self._docker("stop", "-t", "20", name, check=False, timeout=30)
            state["phase"] = "STOPPED"
            state["message"] = "Preview đã dừng; dữ liệu vẫn còn trong container PostgreSQL tạm"
            self._write(state)
            return self.public_state(state)

    def resume(self) -> dict[str, Any]:
        with self._lock:
            state = self._read()
            if state.get("phase") != "STOPPED":
                raise PreviewError("PREVIEW_NOT_STOPPED")
            for key in ("db_container", "app_container"):
                name = str(state.get(key) or "")
                self._assert_owned_container(name)
                self._docker("start", name, timeout=60)
                if key == "db_container":
                    self._wait_postgres(state)
            self._wait_mesflow(state)
            state["phase"] = "READY"
            state["message"] = "Preview đã chạy lại"
            self._write(state)
            return self.public_state(state)

    def delete(self) -> dict[str, Any]:
        with self._lock:
            state = self._read()
            if not state:
                return {"phase": "IDLE", "deleted": True}
            deleted: list[str] = []
            for key in ("app_container", "db_container"):
                name = str(state.get(key) or "")
                if name and self._container_exists(name):
                    self._assert_owned_container(name)
                    self._docker("rm", "-f", name, check=False, timeout=60)
                    deleted.append(name)
            network = str(state.get("network") or "")
            if network and self._docker("network", "inspect", network, check=False).returncode == 0:
                self._assert_owned_network(network)
                self._docker("network", "rm", network, check=False, timeout=30)
                deleted.append(network)
            self.state_file.unlink(missing_ok=True)
            return {"phase": "IDLE", "deleted": True, "resources": deleted}

    def logs(self) -> dict[str, Any]:
        state = self._read()
        app_name = str(state.get("app_container") or "")
        db_name = str(state.get("db_container") or "")
        result = {"app": "", "database": ""}
        if app_name and self._container_exists(app_name):
            self._assert_owned_container(app_name)
            r = self._docker("logs", "--tail", "180", app_name, check=False)
            result["app"] = ((r.stdout or "") + (r.stderr or ""))[-24000:]
        if db_name and self._container_exists(db_name):
            self._assert_owned_container(db_name)
            r = self._docker("logs", "--tail", "80", db_name, check=False)
            result["database"] = ((r.stdout or "") + (r.stderr or ""))[-12000:]
        return result


def shutil_which(command: str) -> str | None:
    from shutil import which
    return which(command)


def register_preview_routes(app, root: Path, app_version: str) -> PreviewManager:
    manager = PreviewManager(root)

    @app.get("/ui-preview")
    def ui_preview_page():
        return render_template(
            "ui_preview.html",
            app_version=app_version,
            presets=PRESETS,
            default_image=manager.default_image(),
        )

    @app.get("/api/ui-preview/status")
    def ui_preview_status():
        return jsonify({"ok": True, "preview": manager.public_state(manager.status())})

    @app.post("/api/ui-preview/start")
    def ui_preview_start():
        try:
            state = manager.start_async(request.get_json(silent=True) or {})
            return jsonify({"ok": True, "preview": state}), 202
        except PreviewError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 409

    @app.post("/api/ui-preview/reseed")
    def ui_preview_reseed():
        try:
            payload = request.get_json(silent=True) or {}
            state = manager.reseed(str(payload.get("preset") or ""))
            return jsonify({"ok": True, "preview": state})
        except PreviewError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 409

    @app.post("/api/ui-preview/stop")
    def ui_preview_stop():
        try:
            return jsonify({"ok": True, "preview": manager.stop()})
        except PreviewError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 409

    @app.post("/api/ui-preview/resume")
    def ui_preview_resume():
        try:
            return jsonify({"ok": True, "preview": manager.resume()})
        except PreviewError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 409

    @app.delete("/api/ui-preview")
    def ui_preview_delete():
        try:
            return jsonify({"ok": True, "preview": manager.delete()})
        except PreviewError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 409

    @app.get("/api/ui-preview/logs")
    def ui_preview_logs():
        try:
            return jsonify({"ok": True, "logs": manager.logs()})
        except PreviewError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 409

    return manager
