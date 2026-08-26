from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
import uuid
import zipfile
from pathlib import Path
from typing import Any

from .evidence import EvidenceStore
from .service import QualificationError
from .store import connect, now

# Same identity convention engine/preview_manager.py's PreviewManager.build()
# uses for its own self_container: an explicit override for anything docker
# can't resolve a container's own hostname from, falling back to
# socket.gethostname() (== the container's own short ID when actually
# running in a container; some arbitrary host name otherwise, which
# `docker network connect` will simply fail to resolve -- handled as the
# bare-host fallback path in deploy() below).
SELF_CONTAINER = os.environ.get("MESFLOW_QA_SELF_CONTAINER") or socket.gethostname()


class DeploymentError(QualificationError):
    pass


def _run(command: list[str], *, input_bytes: bytes | None = None, timeout: int = 180) -> subprocess.CompletedProcess:
    result = subprocess.run(command, input=input_bytes, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            timeout=timeout, check=False)
    if result.returncode:
        raise DeploymentError(f"command failed ({result.returncode}): {' '.join(command[:4])}: "
                              f"{result.stdout.decode('utf-8', 'replace')[-1200:]}")
    return result


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ArtifactDeployment:
    """Deploy one frozen MESFlow image bundle into an isolated Docker namespace."""

    def __init__(self, evidence_root: Path):
        self.conn = connect()
        self.evidence = EvidenceStore(evidence_root)

    @staticmethod
    def inspect_package(path: Path) -> tuple[dict[str, Any], Path, tempfile.TemporaryDirectory]:
        temp = tempfile.TemporaryDirectory(prefix="mesflow-qualification-")
        root = Path(temp.name)
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            required = {"mesflow-release/release.json", "mesflow-release/checksums.txt", "mesflow-release/VERSION.txt"}
            if not required.issubset(names):
                temp.cleanup()
                raise DeploymentError("unsupported artifact: MESFlow image release contract is incomplete")
            for info in archive.infolist():
                target = (root / info.filename).resolve()
                if root.resolve() not in target.parents and target != root.resolve():
                    temp.cleanup()
                    raise DeploymentError("unsafe artifact path")
            archive.extractall(root)
        release_root = root / "mesflow-release"
        manifest = json.loads((release_root / "release.json").read_text(encoding="utf-8"))
        if manifest.get("type") != "mesflow-image-release" or manifest.get("distribution") != "bundle":
            temp.cleanup()
            raise DeploymentError("unsupported artifact release type")
        checks = {}
        for line in (release_root / "checksums.txt").read_text(encoding="utf-8").splitlines():
            digest, filename = line.split(None, 1)
            checks[filename.strip()] = digest
        for filename, expected in checks.items():
            member = release_root / filename
            if not member.is_file() or _sha(member) != expected:
                temp.cleanup()
                raise DeploymentError(f"artifact member checksum mismatch: {filename}")
        image_tar = release_root / str(manifest.get("bundle", ""))
        if not image_tar.is_file():
            temp.cleanup()
            raise DeploymentError("artifact image bundle missing")
        return manifest, image_tar, temp

    def deploy(self, run_id: str, artifact_path: Path, *, fixture_version: str) -> dict[str, Any]:
        run = self.conn.execute("SELECT q.*,a.sha256,a.filename FROM qa_qualification_runs q JOIN qa_artifacts a ON a.id=q.artifact_id WHERE q.id=?", (run_id,)).fetchone()
        if not run or _sha(artifact_path.resolve()) != run["sha256"]:
            raise DeploymentError("artifact bytes do not match qualification run")
        suffix = re.sub(r"[^a-z0-9]", "", run_id.lower())[-12:]
        namespace = f"mesflow-qualification-{suffix}"
        app, db = f"{namespace}-app", f"{namespace}-db"
        network, volume = f"{namespace}-net", f"{namespace}-pg"
        deployment_id = f"dep-{uuid.uuid4().hex}"
        database_identity = f"{db}/mesflow_qa"
        self.conn.execute("""INSERT INTO qa_deployments(id,qualification_run_id,namespace,status,application_container,
          database_container,network_name,volume_name,database_identity,started_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
          (deployment_id, run_id, namespace, "DEPLOYING", app, db, network, volume, database_identity, now()))
        self.conn.commit()
        temp = None
        try:
            manifest, image_tar, temp = self.inspect_package(artifact_path.resolve())
            if str(manifest.get("version")) != str(run["filename"]).removeprefix("MESFlow_").removesuffix(".deploy.zip"):
                raise DeploymentError("artifact filename and manifest version disagree")
            _run(["docker", "load", "-i", str(image_tar)], timeout=300)
            image = str(manifest["image"])
            loaded_id = _run(["docker", "image", "inspect", image, "--format", "{{.Id}}"]).stdout.decode().strip()
            if loaded_id != manifest.get("image_digest") or loaded_id != manifest.get("image_id"):
                raise DeploymentError("ARTIFACT_RUNTIME_IDENTITY_UNPROVEN: loaded image digest differs from manifest")
            _run(["docker", "network", "create", "--label", "mesflow.qualification=true", network])
            _run(["docker", "volume", "create", "--label", "mesflow.qualification=true", volume])
            password = uuid.uuid4().hex
            _run(["docker", "run", "-d", "--name", db, "--network", network,
                  "--label", "mesflow.qualification=true", "-e", "POSTGRES_DB=mesflow_qa",
                  "-e", "POSTGRES_USER=mesflow_qa", "-e", f"POSTGRES_PASSWORD={password}",
                  "-v", f"{volume}:/var/lib/postgresql/data", "postgres:17-alpine"])
            deadline = time.time() + 90
            while time.time() < deadline:
                probe = subprocess.run(["docker", "exec", db, "pg_isready", "-U", "mesflow_qa", "-d", "mesflow_qa"], stdout=subprocess.DEVNULL)
                if probe.returncode == 0:
                    break
                time.sleep(1)
            else:
                raise DeploymentError("qualification PostgreSQL did not become ready")
            database_url = f"postgresql://mesflow_qa:{password}@{db}:5432/mesflow_qa"
            _run(["docker", "run", "-d", "--name", app, "--network", network,
                  "--label", "mesflow.qualification=true", "-p", "127.0.0.1::8080",
                  "-e", "MESFLOW_ENV=test", "-e", f"DATABASE_URL={database_url}",
                  "-e", f"WORKSHOP_DATABASE_URL={database_url}", "-e", "MESFLOW_TEST_AUTO_LOGIN=0",
                  "-e", "MESFLOW_ADMIN_PASSWORD=Admin@123456",
                  "-e", "WORKSHOP_COOKIE_SECURE=0",
                  image])
            port_text = _run(["docker", "port", app, "8080/tcp"]).stdout.decode().strip()
            port = int(port_text.rsplit(":", 1)[1])
            published_url = f"http://127.0.0.1:{port}"
            # `127.0.0.1:<published-port>` only resolves to this sibling
            # container when the caller is the bare host itself (published
            # ports are a host-daemon concept). When this process is itself
            # running inside a container talking to the host daemon over a
            # mounted docker.sock (Docker-outside-of-Docker -- the real,
            # packaged way QA Center runs; see compose.yml's docker.sock +
            # host.docker.internal wiring), 127.0.0.1 inside THIS container
            # is not the host's 127.0.0.1, so the poll below would time out
            # against a target that is actually healthy (confirmed by hand:
            # the app container's own logs already showed a ready response
            # while this loop kept failing). engine/preview_manager.py hit
            # the identical problem for the UI Preview Lab and solved it by
            # joining the spawned network directly and addressing the
            # sibling by container name -- reused here rather than inventing
            # a second solution to the same problem in the same codebase.
            joined_network = False
            try:
                _run(["docker", "network", "connect", network, SELF_CONTAINER])
                joined_network = True
            except DeploymentError:
                joined_network = False  # bare-host invocation (e.g. a developer running the CLI directly): fall back below
            target_url = f"http://{app}:8080" if joined_network else published_url
            deadline = time.time() + 150
            ready = None
            while time.time() < deadline:
                state = _run(["docker", "inspect", app, "--format", "{{.State.Status}}"]).stdout.decode().strip()
                if state == "exited":
                    raise DeploymentError(f"deployed artifact exited before readiness: {self.logs(app, db).get(app, '')[-1600:]}")
                try:
                    with urllib.request.urlopen(f"{target_url}/api/system/ready", timeout=3) as response:
                        ready = json.load(response)
                    if ready.get("ok"):
                        break
                except Exception:
                    time.sleep(1)
            if not ready or not ready.get("ok"):
                raise DeploymentError("deployed artifact did not become ready")
            container_image_id = _run(["docker", "inspect", app, "--format", "{{.Image}}"]).stdout.decode().strip()
            if container_image_id != loaded_id:
                raise DeploymentError("ARTIFACT_RUNTIME_IDENTITY_UNPROVEN: running container image differs")
            runtime = {"artifact_sha256": run["sha256"], "manifest": manifest, "loaded_image_id": loaded_id,
                       "container_image_id": container_image_id, "container": app, "database_container": db,
                       "target_url": target_url, "published_url": published_url, "joined_network": joined_network,
                       "ready": ready, "host": socket.gethostname(), "fixture_version": fixture_version}
            self.evidence.write_json(run_id, "runtime-identity.json", runtime, kind="RUNTIME_IDENTITY")
            self.conn.execute("UPDATE qa_deployments SET status='READY',target_url=?,manifest_json=?,runtime_json=?,ready_at=? WHERE id=?",
                              (target_url, json.dumps(manifest), json.dumps(runtime), now(), deployment_id))
            self.conn.commit()
            return {"id": deployment_id, "namespace": namespace, "app_container": app, "db_container": db,
                    "network": network, "volume": volume, "target_url": target_url,
                    "database_identity": database_identity, "runtime": runtime}
        except Exception as exc:
            logs = self.logs(app, db)
            self.evidence.write_json(run_id, "deployment-failure.json", {"error": str(exc), "logs": logs}, kind="DEPLOYMENT_FAILURE")
            self.conn.execute("UPDATE qa_deployments SET status='FAILED',error=? WHERE id=?", (str(exc), deployment_id))
            self.conn.commit()
            self.destroy(namespace)
            raise
        finally:
            if temp:
                temp.cleanup()

    @staticmethod
    def logs(app: str, db: str) -> dict[str, str]:
        values = {}
        for name in (app, db):
            result = subprocess.run(["docker", "logs", "--tail", "300", name], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            values[name] = result.stdout.decode("utf-8", "replace")
        return values

    def destroy(self, namespace: str) -> None:
        if not namespace.startswith("mesflow-qualification-"):
            raise DeploymentError("refusing unsafe cleanup namespace")
        for suffix in ("app", "db"):
            subprocess.run(["docker", "rm", "-f", f"{namespace}-{suffix}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # Best-effort: only actually connected (joined_network=True in deploy())
        # when this process itself runs as a container docker can resolve.
        # A network still attached to us would otherwise make `network rm`
        # fail and leak it, same reason preview_manager.py disconnects
        # itself before removing its own preview network.
        subprocess.run(["docker", "network", "disconnect", "-f", f"{namespace}-net", SELF_CONTAINER],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["docker", "network", "rm", f"{namespace}-net"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["docker", "volume", "rm", f"{namespace}-pg"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
