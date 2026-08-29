"""UI Preview Lab: isolated MESFlow environments for deterministic UI review
(requirement 1).

Architecture (never deviates from this):

    mesflow-ui-preview-<id>  -- MESFlow image, its own host port (18080+)
        DATABASE_URL -> mesflow-ui-db-<id> / mesflow_ui_<id>
    mesflow-ui-db-<id>       -- its own PostgreSQL, its own database
    mesflow-ui-net-<id>      -- its own Docker network

Never touches mesflow-app / mesflow-postgres / the real `mesflow` database.
Every resource this module creates carries the Docker label
``com.mesflow.qa.preview=1`` and the app container additionally carries
``MESFLOW_UI_PREVIEW=1`` in its environment. Every *destructive* operation
(stop/remove container, remove network, drop database) re-checks those
guards immediately before acting and refuses (fail closed) if a guard is
missing -- see ``preview_guard``.

No function in this module runs an arbitrary/user-supplied docker command;
every subcommand is a fixed, allowlisted verb.
"""
from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable
from urllib.parse import urlparse

import requests

from . import qa_store
from .preview import presets as preview_presets
from .preview import seed as preview_seed
from .preview_guard import (
    PREVIEW_DB_PREFIX,
    PREVIEW_LABEL_KEY,
    PREVIEW_LABEL_VALUE,
    PREVIEW_VOLUME_ID_LABEL_KEY,
    REQUIRED_ENV_FLAG,
    PreviewSafetyError,
    assert_preview_db_name,
    assert_preview_env,
    assert_preview_label,
    assert_preview_volume_label,
)


def _env_int(*names: str, default: str) -> int:
    for name in names:
        value = os.environ.get(name)
        if value:
            return int(value)
    return int(default)


# MESFLOW_PREVIEW_* is the current name; MESFLOW_QA_PREVIEW_* (this module's
# original names) is accepted too so an already-deployed .env keeps working.
DEFAULT_PORT_RANGE_START = _env_int("MESFLOW_PREVIEW_PORT_START", "MESFLOW_QA_PREVIEW_PORT_START", default="18080")
DEFAULT_PORT_RANGE_END = _env_int("MESFLOW_PREVIEW_PORT_END", "MESFLOW_QA_PREVIEW_PORT_END", default="18180")
# No hardcoded fallback tag here on purpose: MESFlow images are Build-Once /
# version-pinned (mesflow-app:71.0.0.52, never mesflow-app:latest -- see
# reports/BUILD_ONCE_LOCAL_ARCHITECTURE.md), so a fixed default would just be
# a tag that never exists. When MESFLOW_QA_PREVIEW_IMAGE isn't set, resolve()
# below clones whatever image the real mesflow-app container is running.
DEFAULT_IMAGE = os.environ.get("MESFLOW_QA_PREVIEW_IMAGE", "")
DB_IMAGE = os.environ.get("MESFLOW_QA_PREVIEW_DB_IMAGE", "postgres:17-alpine")
RUNNING_APP_CONTAINER = os.environ.get("MESFLOW_QA_APP_CONTAINER", "mesflow-app")

# Host interface the preview's published port is bound to. Defaults to
# loopback-only -- a preview environment carries a freshly generated admin
# password but is still a full MESFlow instance, so it is never exposed
# beyond localhost unless an operator explicitly opts in (requirement 6:
# "không expose 0.0.0.0 mặc định nếu không cần"). Internal readiness/coverage
# checks never depend on this -- see internal_base_url() -- they go over the
# preview's own Docker network directly by container name, regardless of
# what this is set to.
PREVIEW_BIND_HOST = os.environ.get("MESFLOW_PREVIEW_BIND_HOST", "127.0.0.1")
# What a human's browser should be told to open ("Open Preview"). Separate
# from PREVIEW_BIND_HOST so a operator reaching QA Center over LAN/remote
# can set this to that host's real IP without having to bind the container
# port to 0.0.0.0 (bind host and the address a *browser* dials are different
# concerns). Falls back to the bind host, except when that bind host is the
# wildcard address (nothing dials 0.0.0.0), where 127.0.0.1 is the only sane
# default left.
PREVIEW_PUBLIC_HOST = os.environ.get("MESFLOW_PREVIEW_PUBLIC_HOST") or (
    "127.0.0.1" if PREVIEW_BIND_HOST == "0.0.0.0" else PREVIEW_BIND_HOST
)

# Fixed, known admin login for every preview environment -- was
# secrets.token_urlsafe(12) (a different password every time, only
# discoverable via the API/UI). A preview is already isolated (fresh DB,
# fresh network, random DB password, guarded by com.mesflow.qa.preview=1)
# so a predictable admin password costs nothing and lets a tester log in
# without hunting for it first. 10 chars exactly satisfies MESFlow's own
# `MESFLOW_ADMIN_PASSWORD must be >= 10 characters` production bootstrap
# check. Still overridable for anyone who wants a real random one back.
PREVIEW_ADMIN_PASSWORD = os.environ.get("MESFLOW_PREVIEW_ADMIN_PASSWORD", "1234567890")


class PreviewNotFoundError(Exception):
    pass


class DockerError(Exception):
    pass


class NoDefaultImageError(Exception):
    pass


# --------------------------------------------------------------------------
# Docker CLI wrapper -- a small, fixed allowlist of subcommands. The runner
# is injectable so unit tests never need a real Docker daemon.
# --------------------------------------------------------------------------

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def _default_runner(args: list[str], timeout: int = 60) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


class DockerCLI:
    def __init__(self, runner: Runner | None = None):
        self._run = runner or _default_runner

    def _exec(self, args: list[str], timeout: int = 60) -> str:
        result = self._run(["docker", *args], timeout=timeout)
        if result.returncode != 0:
            raise DockerError(f"docker {' '.join(args)} failed: {(result.stderr or '').strip()}")
        return result.stdout

    def labels_of_container(self, name: str) -> dict[str, str]:
        out = self._exec(["inspect", "--format", "{{json .Config.Labels}}", name])
        return json.loads(out.strip() or "null") or {}

    def labels_of_network(self, name: str) -> dict[str, str]:
        out = self._exec(["network", "inspect", name, "--format", "{{json .Labels}}"])
        return json.loads(out.strip() or "null") or {}

    def env_of_container(self, name: str) -> dict[str, str]:
        out = self._exec(["inspect", "--format", "{{json .Config.Env}}", name])
        pairs = json.loads(out.strip() or "[]") or []
        env: dict[str, str] = {}
        for item in pairs:
            if "=" in item:
                k, v = item.split("=", 1)
                env[k] = v
        return env

    def status_of_container(self, name: str) -> str:
        try:
            out = self._exec(["inspect", "--format", "{{.State.Status}}", name])
            return out.strip()
        except DockerError:
            return "MISSING"

    def image_of_container(self, name: str) -> str:
        """The exact image:tag a running container was started from --
        used to default a preview to whatever mesflow-app is running now."""
        out = self._exec(["inspect", "--format", "{{.Config.Image}}", name])
        return out.strip()

    def logs_of_container(self, name: str, tail: int) -> str:
        """Best-effort tail of a container's combined stdout/stderr, for
        debugging a preview from the UI. Tolerant, not _exec: a container
        that never started or was already removed has nothing useful to
        raise about -- an empty string is the honest answer, not an
        error."""
        result = self._run(["docker", "logs", "--tail", str(tail), name], timeout=15)
        return ((result.stdout or "") + (result.stderr or ""))

    def create_network(self, name: str) -> None:
        self._exec(["network", "create", "--label", f"{PREVIEW_LABEL_KEY}={PREVIEW_LABEL_VALUE}", name], timeout=30)

    def create_volume(self, name: str, labels: dict[str, str]) -> None:
        args = ["volume", "create"]
        for k, v in labels.items():
            args += ["--label", f"{k}={v}"]
        args.append(name)
        self._exec(args, timeout=30)

    def labels_of_volume(self, name: str) -> dict[str, str]:
        out = self._exec(["volume", "inspect", name, "--format", "{{json .Labels}}"])
        return json.loads(out.strip() or "null") or {}

    def safe_remove_volume(self, name: str, env_id: str) -> None:
        """Guarded like every other destructive op (requirement 5/7): refuses
        unless the volume carries both the generic preview label AND an id
        label matching this exact environment."""
        labels = self.labels_of_volume(name)
        assert_preview_volume_label(labels, env_id)
        self._exec(["volume", "rm", name], timeout=30)

    def run_container(
        self, *, name: str, image: str, network: str, env: dict[str, str],
        ports: dict[int, int] | None = None, labels: dict[str, str] | None = None,
        volumes: dict[str, str] | None = None, bind_host: str = "127.0.0.1",
        extra_args: list[str] | None = None,
    ) -> str:
        labels = dict(labels or {})
        labels.setdefault(PREVIEW_LABEL_KEY, PREVIEW_LABEL_VALUE)
        args = ["run", "-d", "--name", name, "--network", network]
        for k, v in labels.items():
            args += ["--label", f"{k}={v}"]
        for k, v in (env or {}).items():
            args += ["-e", f"{k}={v}"]
        for host_port, container_port in (ports or {}).items():
            args += ["-p", f"{bind_host}:{host_port}:{container_port}"]
        for volume_name, container_path in (volumes or {}).items():
            args += ["-v", f"{volume_name}:{container_path}"]
        if extra_args:
            args += extra_args
        args.append(image)
        return self._exec(args, timeout=120).strip()

    def exec_in_container(self, name: str, cmd: list[str], timeout: int = 30) -> str:
        labels = self.labels_of_container(name)
        assert_preview_label(labels)
        return self._exec(["exec", name, *cmd], timeout=timeout)

    def safe_stop_container(self, name: str) -> None:
        labels = self.labels_of_container(name)
        assert_preview_label(labels)
        self._exec(["stop", name], timeout=30)

    def safe_start_container(self, name: str) -> None:
        labels = self.labels_of_container(name)
        assert_preview_label(labels)
        self._exec(["start", name], timeout=30)

    def safe_remove_container(self, name: str) -> None:
        labels = self.labels_of_container(name)
        assert_preview_label(labels)
        self._exec(["rm", "-f", name], timeout=30)

    def safe_remove_network(self, name: str) -> None:
        labels = self.labels_of_network(name)
        assert_preview_label(labels)
        self._exec(["network", "rm", name], timeout=30)

    def connect_network(self, network: str, container: str) -> None:
        self._exec(["network", "connect", network, container], timeout=30)

    def disconnect_network(self, network: str, container: str, force: bool = True) -> None:
        args = ["network", "disconnect"]
        if force:
            args.append("-f")
        args += [network, container]
        try:
            self._exec(args, timeout=30)
        except DockerError:
            pass  # already disconnected / container gone -- deletion must not get stuck here


# --------------------------------------------------------------------------
# Manager
# --------------------------------------------------------------------------

@dataclass
class PreviewManager:
    docker: DockerCLI
    self_container: str

    @classmethod
    def build(cls, docker: DockerCLI | None = None, self_container: str | None = None) -> "PreviewManager":
        return cls(
            docker=docker or DockerCLI(),
            self_container=self_container or os.environ.get("MESFLOW_QA_SELF_CONTAINER") or socket.gethostname(),
        )

    # -- persistence -----------------------------------------------------

    def _conn(self):
        return qa_store.connect()

    def _row(self, r) -> dict[str, Any]:
        d = dict(r)
        try:
            d["manifest"] = json.loads(d.get("manifest_json") or "{}")
        except (TypeError, ValueError):
            d["manifest"] = {}
        return d

    def get(self, env_id: str) -> dict[str, Any]:
        row = self._conn().execute("SELECT * FROM preview_environments WHERE id=?", (env_id,)).fetchone()
        if not row:
            raise PreviewNotFoundError(env_id)
        return self._row(row)

    def list(self) -> list[dict[str, Any]]:
        rows = self._conn().execute("SELECT * FROM preview_environments ORDER BY created_at DESC").fetchall()
        return [self._row(r) for r in rows]

    def _used_ports(self) -> set[int]:
        rows = self._conn().execute("SELECT port FROM preview_environments WHERE status!='DELETED'").fetchall()
        return {int(r["port"]) for r in rows}

    def allocate_port(self) -> int:
        used = self._used_ports()
        for port in range(DEFAULT_PORT_RANGE_START, DEFAULT_PORT_RANGE_END):
            if port in used:
                continue
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
        raise RuntimeError("No free preview port available in range")

    def _set(self, env_id: str, **fields: Any) -> None:
        fields["updated_at"] = datetime.now().isoformat(timespec="seconds")
        cols = ",".join(f"{k}=?" for k in fields)
        self._conn().execute(f"UPDATE preview_environments SET {cols} WHERE id=?", (*fields.values(), env_id))
        self._conn().commit()

    # -- guard check before any destructive op ---------------------------

    def _guard_destructive(self, env: dict[str, Any]) -> None:
        assert_preview_db_name(env["db_name"])
        try:
            container_env = self.docker.env_of_container(env["app_container"])
        except DockerError:
            container_env = {}
        assert_preview_env(container_env)
        labels = self.docker.labels_of_container(env["app_container"])
        assert_preview_label(labels)

    # -- lifecycle ---------------------------------------------------------

    def create(self, preset: str, *, image: str | None = None) -> dict[str, Any]:
        """Fully synchronous create: provision + wait for READY. Used by the
        CLI/smoke test. HTTP callers should use ``begin_create``/``finish_create``
        instead so the request returns immediately with a pollable env_id."""
        env_id = self.begin_create(preset, image=image)
        self.finish_create(env_id)
        return self.get(env_id)

    def default_image(self) -> str:
        """Resolve which mesflow-app image a new preview should clone when
        the caller didn't ask for a specific one: MESFLOW_QA_PREVIEW_IMAGE if
        set, otherwise whatever image the real ``mesflow-app`` container is
        currently running (never a hardcoded tag -- see the DEFAULT_IMAGE
        comment above)."""
        if DEFAULT_IMAGE:
            return DEFAULT_IMAGE
        try:
            image = self.docker.image_of_container(RUNNING_APP_CONTAINER)
        except DockerError as exc:
            raise NoDefaultImageError(
                f"No preview image specified and could not read the image "
                f"'{RUNNING_APP_CONTAINER}' is running ({exc}). Pass an explicit "
                f"'image' (e.g. mesflow-app:71.0.0.52) or set MESFLOW_QA_PREVIEW_IMAGE."
            ) from exc
        if not image:
            raise NoDefaultImageError(f"Container '{RUNNING_APP_CONTAINER}' reported an empty image")
        return image

    def begin_create(self, preset: str, *, image: str | None = None) -> str:
        """Fast path: validate + allocate the environment row (status
        CREATING) and return its id immediately. Call ``finish_create`` next
        (typically on a background thread) to actually stand up Docker
        resources and seed the database."""
        preview_presets.validate(preset)
        env_id = uuid.uuid4().hex[:8]
        image = image or self.default_image()
        db_name = f"{PREVIEW_DB_PREFIX}{env_id}"
        assert_preview_db_name(db_name)  # defense in depth, should always pass
        network = f"mesflow-ui-net-{env_id}"
        app_container = f"mesflow-ui-preview-{env_id}"
        db_container = f"mesflow-ui-db-{env_id}"
        volume = f"mesflow-ui-data-{env_id}"
        port = self.allocate_port()
        db_password = secrets.token_urlsafe(18)
        admin_password = PREVIEW_ADMIN_PASSWORD
        now = datetime.now().isoformat(timespec="seconds")

        self._conn().execute(
            """INSERT INTO preview_environments(id,image,preset,port,status,db_name,app_container,
                   db_container,network,volume,admin_password,db_password,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (env_id, image, preset, port, "CREATING", db_name, app_container, db_container,
             network, volume, admin_password, db_password, now, now),
        )
        self._conn().commit()
        return env_id

    def finish_create(self, env_id: str) -> dict[str, Any]:
        """The slow part of create(): stand up the network/db/app containers,
        wait for readiness, then seed. Safe to run on a background thread."""
        env = self.get(env_id)
        image, preset = env["image"], env["preset"]
        db_name, network = env["db_name"], env["network"]
        app_container, db_container, volume = env["app_container"], env["db_container"], env["volume"]
        db_password, admin_password = env["db_password"], env["admin_password"]
        port = env["port"]

        try:
            self.docker.create_network(network)
            # Named volume, not the container's own writable layer and never
            # --tmpfs: Stop must not silently lose data, and Delete is the
            # only operation allowed to remove it (requirement 7).
            self.docker.create_volume(volume, labels={
                PREVIEW_LABEL_KEY: PREVIEW_LABEL_VALUE,
                PREVIEW_VOLUME_ID_LABEL_KEY: env_id,
            })
            self.docker.run_container(
                name=db_container, image=DB_IMAGE, network=network,
                env={"POSTGRES_DB": db_name, "POSTGRES_USER": "mesflow", "POSTGRES_PASSWORD": db_password},
                labels={PREVIEW_LABEL_KEY: PREVIEW_LABEL_VALUE},
                volumes={volume: "/var/lib/postgresql/data"},
            )
            self._wait_db_ready(db_container, db_name)

            self.docker.connect_network(network, self.self_container)
            database_url = f"postgresql://mesflow:{db_password}@{db_container}:5432/{db_name}"

            self.docker.run_container(
                name=app_container, image=image, network=network,
                env={
                    REQUIRED_ENV_FLAG: "1",
                    "MESFLOW_ENV": "production",
                    "DATABASE_URL": database_url,
                    "MESFLOW_ADMIN_USERNAME": "admin",
                    "MESFLOW_ADMIN_PASSWORD": admin_password,
                    # mesflow.core.config refuses to boot in MESFLOW_ENV=production
                    # without a real secret key. Generated fresh per preview and
                    # never persisted -- nothing outside this container's own
                    # session cookies needs it, and the app container is never
                    # recreated (stop/start reuses it), so it only has to exist
                    # at `docker run` time.
                    "MESFLOW_SECRET_KEY": secrets.token_hex(32),
                },
                ports={port: 8080},
                bind_host=PREVIEW_BIND_HOST,
                labels={PREVIEW_LABEL_KEY: PREVIEW_LABEL_VALUE},
            )
            mesflow_version = self._wait_app_ready(app_container)
            self._set(env_id, status="SEEDING", mesflow_version=mesflow_version)
            manifest = preview_seed.run_seed(database_url, preset)
            self._set(
                env_id, status="READY", mesflow_version=mesflow_version,
                seed_version=str(manifest.get("seed_version", "")),
                manifest_json=json.dumps(manifest, ensure_ascii=False, default=str),
            )
        except Exception as exc:
            self._set(env_id, status="FAILED", last_error=str(exc))
            raise
        return self.get(env_id)

    def _wait_db_ready(self, db_container: str, db_name: str, timeout: int = 60) -> None:
        deadline = time.time() + timeout
        last_err = ""
        while time.time() < deadline:
            try:
                self.docker.exec_in_container(db_container, ["pg_isready", "-U", "mesflow", "-d", db_name], timeout=10)
                return
            except DockerError as exc:
                last_err = str(exc)
                time.sleep(2)
        raise DockerError(f"Preview database never became ready: {last_err}")

    def _wait_app_ready(self, app_container: str, timeout: int = 90) -> str:
        # Reached over the preview's own Docker network by container name --
        # self_container was connect_network()'d onto it just before this is
        # called -- never through the published host port. This is what
        # makes readiness/health checks independent of PREVIEW_BIND_HOST: a
        # preview bound to 127.0.0.1-only still becomes READY correctly.
        url = f"http://{app_container}:8080/api/system/ready"
        deadline = time.time() + timeout
        last_err = ""
        while time.time() < deadline:
            try:
                resp = requests.get(url, timeout=5)
                if resp.status_code == 200 and resp.json().get("ok"):
                    return str(resp.json().get("version", ""))
            except Exception as exc:  # noqa: BLE001 - network probing, keep polling
                last_err = str(exc)
            time.sleep(2)
        raise DockerError(f"Preview app never became ready: {last_err}")

    def stop(self, env_id: str) -> dict[str, Any]:
        env = self.get(env_id)
        self._guard_destructive(env)
        self.docker.safe_stop_container(env["app_container"])
        self.docker.safe_stop_container(env["db_container"])
        self._set(env_id, status="STOPPED")
        return self.get(env_id)

    def start(self, env_id: str) -> dict[str, Any]:
        """Fully synchronous start. HTTP callers should use
        ``begin_start``/``finish_start`` to avoid blocking the request while
        the app container becomes ready (can take up to ~90s)."""
        self.begin_start(env_id)
        return self.finish_start(env_id)

    def begin_start(self, env_id: str) -> dict[str, Any]:
        env = self.get(env_id)
        assert_preview_db_name(env["db_name"])
        self._set(env_id, status="STARTING")
        return self.get(env_id)

    def finish_start(self, env_id: str) -> dict[str, Any]:
        env = self.get(env_id)
        try:
            # `start`, never `run` -- reuses the existing db_container (and
            # its named volume) and app_container as-is. Nothing here can
            # lose data: see test_stop_then_start_never_recreates_containers.
            self.docker.safe_start_container(env["db_container"])
            self._wait_db_ready(env["db_container"], env["db_name"])
            self.docker.safe_start_container(env["app_container"])
            mesflow_version = self._wait_app_ready(env["app_container"])
        except Exception as exc:
            self._set(env_id, status="FAILED", last_error=str(exc))
            raise
        self._set(env_id, status="READY", mesflow_version=mesflow_version)
        return self.get(env_id)

    def reset_reseed(self, env_id: str, preset: str | None = None) -> dict[str, Any]:
        """Switch testcase in place (the whole point of a shared preview
        environment, requirement 1): TRUNCATE the domain/business tables and
        seed a new preset into the SAME app container / DB container /
        network / port / volume / preview_id / DB credentials -- never a
        `docker run`, never a new environment row. Pass `preset` to change
        which dataset this environment now holds (e.g. FULL_UI -> EDGE_CASES
        without ever tearing anything down); omit it to just re-roll the
        environment's current preset. Refuses (fail closed, no partial
        change) if `preset` isn't a real preset key -- checked before
        anything is touched."""
        if preset is not None:
            preview_presets.validate(preset)
        env = self.get(env_id)
        self._guard_destructive(env)
        target_preset = preset or env["preset"]
        self._set(env_id, status="SEEDING", preset=target_preset)
        db_password = env["db_password"]
        database_url = f"postgresql://mesflow:{db_password}@{env['db_container']}:5432/{env['db_name']}"
        try:
            preview_seed.wipe_runtime_data(database_url)
            manifest = preview_seed.run_seed(database_url, target_preset)
        except Exception as exc:
            self._set(env_id, status="FAILED", last_error=str(exc))
            raise
        self._set(
            env_id, status="READY", seed_version=str(manifest.get("seed_version", "")),
            manifest_json=json.dumps(manifest, ensure_ascii=False, default=str),
        )
        return self.get(env_id)

    def delete(self, env_id: str) -> None:
        env = self.get(env_id)
        self._guard_destructive(env)
        try:
            self.docker.safe_remove_container(env["app_container"])
        except DockerError:
            pass
        try:
            self.docker.safe_remove_container(env["db_container"])
        except DockerError:
            pass
        self.docker.disconnect_network(env["network"], self.self_container)
        try:
            self.docker.safe_remove_network(env["network"])
        except DockerError:
            pass
        volume = env.get("volume") or ""
        if volume:
            try:
                self.docker.safe_remove_volume(volume, env_id)
            except DockerError:
                pass
        self._set(env_id, status="DELETED")

    def base_url(self, env_id: str) -> str:
        """What a human's browser should open. Never the internal container
        address -- see internal_base_url()."""
        env = self.get(env_id)
        return f"http://{PREVIEW_PUBLIC_HOST}:{env['port']}"

    def internal_base_url(self, env_id: str) -> str:
        """URL reachable from *inside* the QA Center container (used by
        readiness checks and the coverage runner): the preview's own
        container name over the Docker network QA Center joined in
        finish_create(), never the published host port. Correct regardless
        of PREVIEW_BIND_HOST/PREVIEW_PUBLIC_HOST."""
        env = self.get(env_id)
        return f"http://{env['app_container']}:8080"

    def runtime_state(self, env_id: str) -> dict[str, Any]:
        """Read-only Docker/runtime snapshot for the "Làm mới" refresh action
        (requirement 2): status/health *as Docker sees it right now*, purely
        additive to the persisted row -- this never calls run/stop/start/rm/
        create and never writes to the database, so refreshing can never
        recreate, restart, reseed, or delete anything.

        Never raises: a preview host without a reachable Docker daemon (e.g.
        a sandboxed test run) just gets "UNKNOWN" back instead of a 500, so
        callers -- including plain HTTP list/get routes -- can always merge
        this in unconditionally."""
        try:
            env = self.get(env_id)
            app_status = self.docker.status_of_container(env["app_container"])
            db_status = self.docker.status_of_container(env["db_container"])
            health: bool | None = None
            if app_status == "running":
                try:
                    resp = requests.get(f"{self.internal_base_url(env_id)}/api/system/ready", timeout=3)
                    health = resp.status_code == 200 and bool(resp.json().get("ok"))
                except Exception:  # noqa: BLE001 - health probe, never raise
                    health = False
            return {"app_status": app_status, "db_status": db_status, "health": health}
        except Exception:  # noqa: BLE001 - refresh must never 500 the page
            return {"app_status": "UNKNOWN", "db_status": "UNKNOWN", "health": None}

    def logs(self, env_id: str, tail: int = 200) -> dict[str, str]:
        """Best-effort tail of a preview's app + db container logs, for
        debugging from the UI. Read-only (never run/stop/start/rm/create),
        but still runs the same ownership guard as a destructive op before
        touching either container -- this never dumps logs from a
        container this module didn't create. Never raises: a container
        that's gone or a Docker daemon that's unreachable just yields an
        empty string for that side, matching runtime_state()'s own
        never-500 convention."""
        env = self.get(env_id)
        try:
            self._guard_destructive(env)
        except PreviewSafetyError:
            return {"app": "", "database": ""}
        result = {"app": "", "database": ""}
        try:
            result["app"] = self.docker.logs_of_container(env["app_container"], tail)[-24000:]
        except Exception:  # noqa: BLE001 - best-effort debug output only
            pass
        try:
            result["database"] = self.docker.logs_of_container(env["db_container"], max(40, tail // 2))[-12000:]
        except Exception:  # noqa: BLE001 - best-effort debug output only
            pass
        return result
