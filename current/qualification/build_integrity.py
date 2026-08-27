"""build_integrity: the first production-policy suite (spec section 2 of the
"software-only production certification" phase). Verifies the artifact
under qualification is internally consistent -- BEFORE any expensive
isolated deploy is attempted -- so a corrupted/mislabeled artifact fails
fast and cheap rather than burning minutes standing up Postgres+app only to
fail later for an unrelated reason.

Deliberately reuses, not reimplements:
  - ArtifactDeployment.inspect_package() for the zip-slip-safe extraction,
    the required-file check, and the per-member checksums.txt validation
    (already the single source of truth for "is this a well-formed MESFlow
    release bundle" -- see deployment.py).
  - The same spirit as scripts/check-version-sync.sh in the MESFlow repo
    (VERSION.txt / release.json / compose.yml must all agree on one
    version) -- adapted here to the artifact's OWN packaged copies of
    those files (check-version-sync.sh itself only ever inspects the live
    checked-out source tree, not a frozen artifact, so it cannot be shelled
    out to directly for this purpose; the same three-way-agreement check it
    encodes is reproduced against the artifact's own files instead of
    being reimplemented from scratch).
"""
from __future__ import annotations

import re
import uuid
import zipfile
from pathlib import Path
from typing import Any

from .deployment import ArtifactDeployment, DeploymentError, _run, _sha
from .evidence import EvidenceStore
from .scenario_runner import ScenarioRunner
from .store import connect, now


class BuildIntegrityRunner:
    def __init__(self, evidence_root: Path):
        self.conn = connect()
        self.evidence = EvidenceStore(evidence_root)
        self._runner = ScenarioRunner(evidence_root, scenario_version="build-integrity-v1",
                                      driver="BUILD", evidence_kind="BUILD_INTEGRITY_EVIDENCE")

    def run(self, run_id: str, artifact_path: Path, *, load_and_verify_runtime_identity: bool = True) -> dict[str, Any]:
        artifact_path = artifact_path.resolve()
        suite_id = f"suite-{uuid.uuid4().hex}"
        self.conn.execute("""INSERT INTO qa_suite_runs(id,qualification_run_id,suite_key,layer,required,status,
          started_at,command_json) VALUES(?,?,'build_integrity','build',1,'RUNNING',?,'[]')""",
          (suite_id, run_id, now()))
        self.conn.commit()

        row = self.conn.execute(
            "SELECT a.sha256,a.filename FROM qa_qualification_runs q JOIN qa_artifacts a ON a.id=q.artifact_id WHERE q.id=?",
            (run_id,)).fetchone()
        if not row:
            raise DeploymentError("unknown qualification run for build_integrity")
        registered_sha256, registered_filename = row["sha256"], row["filename"]

        results: list[dict[str, Any]] = []

        def scenario(key: str, fn) -> None:
            results.append(self._runner.run(suite_id, run_id, key, fn))

        def check_sha256_matches_registration():
            actual = _sha(artifact_path)
            if actual != registered_sha256:
                raise AssertionError(f"artifact bytes sha256 {actual} != registered {registered_sha256}")
            return {"sha256": actual}

        def check_filename_matches_registration():
            if artifact_path.name != registered_filename:
                raise AssertionError(f"artifact filename {artifact_path.name!r} != registered {registered_filename!r}")
            return {"filename": artifact_path.name}

        def check_zip_not_corrupted():
            with zipfile.ZipFile(artifact_path) as archive:
                bad = archive.testzip()
                if bad is not None:
                    raise AssertionError(f"corrupt member in archive: {bad}")
                names = archive.namelist()
                if not names:
                    raise AssertionError("archive is empty")
            return {"member_count": len(names)}

        manifest_holder: dict[str, Any] = {}

        def check_manifest_required_fields_and_checksums():
            # Reuses inspect_package() wholesale: zip-slip guard, the
            # required {release.json, checksums.txt, VERSION.txt} set, and
            # the per-member sha256 checksums.txt validation. A raised
            # DeploymentError here IS the finding -- not caught locally, it
            # propagates up through scenario()'s own try/except.
            manifest, image_tar, temp = ArtifactDeployment.inspect_package(artifact_path)
            try:
                required_manifest_fields = {"type", "distribution", "image", "image_digest", "image_id",
                                            "version", "bundle"}
                missing = required_manifest_fields - manifest.keys()
                if missing:
                    raise AssertionError(f"release.json missing required fields: {sorted(missing)}")
                if manifest.get("type") != "mesflow-image-release" or manifest.get("distribution") != "bundle":
                    raise AssertionError(f"unexpected manifest type/distribution: {manifest.get('type')}/{manifest.get('distribution')}")
                if manifest.get("image_digest") != manifest.get("image_id"):
                    raise AssertionError("manifest image_digest and image_id disagree -- ambiguous runtime identity")
                if not image_tar.is_file() or image_tar.stat().st_size < 1024:
                    raise AssertionError(f"packaged image bundle missing or suspiciously small: {image_tar}")
                manifest_holder["manifest"] = manifest
                return {"manifest_fields": sorted(manifest.keys()), "bundle_size_bytes": image_tar.stat().st_size}
            finally:
                temp.cleanup()

        def check_filename_manifest_version_agree():
            manifest = manifest_holder.get("manifest") or {}
            filename_version = re.sub(r"^MESFlow_", "", artifact_path.stem).removesuffix(".deploy")
            if str(manifest.get("version")) != filename_version:
                raise AssertionError(f"filename encodes version {filename_version!r} but manifest.version is {manifest.get('version')!r}")
            return {"filename_version": filename_version, "manifest_version": manifest.get("version")}

        def check_internal_version_declarations_agree():
            # Same three-way agreement scripts/check-version-sync.sh enforces
            # for the live source tree (VERSION.txt / release.json /
            # compose.yml), reproduced here against the artifact's own
            # packaged copies rather than shelling out to a script that
            # only knows how to look at a checked-out working tree.
            with zipfile.ZipFile(artifact_path) as archive:
                version_txt = archive.read("mesflow-release/VERSION.txt").decode("utf-8").strip()
                compose_yml = archive.read("mesflow-release/compose.yml").decode("utf-8")
            manifest = manifest_holder.get("manifest") or {}
            manifest_version = str(manifest.get("version"))
            if version_txt != manifest_version:
                raise AssertionError(f"VERSION.txt ({version_txt!r}) != release.json version ({manifest_version!r})")
            compose_default_match = re.search(r"MESFLOW_IMAGE:-mesflow-app:([0-9.]+)", compose_yml)
            if not compose_default_match:
                raise AssertionError("compose.yml has no MESFLOW_IMAGE default fallback to check")
            if compose_default_match.group(1) != manifest_version:
                raise AssertionError(f"compose.yml's default image tag ({compose_default_match.group(1)!r}) "
                                     f"!= release.json version ({manifest_version!r})")
            return {"version_txt": version_txt, "compose_default_tag": compose_default_match.group(1)}

        def check_runtime_image_identity_after_load():
            if not load_and_verify_runtime_identity:
                return {"skipped": "load_and_verify_runtime_identity=False"}
            manifest, image_tar, temp = ArtifactDeployment.inspect_package(artifact_path)
            try:
                _run(["docker", "load", "-i", str(image_tar)], timeout=300)
                image = str(manifest["image"])
                loaded_id = _run(["docker", "image", "inspect", image, "--format", "{{.Id}}"]).stdout.decode().strip()
                if loaded_id != manifest.get("image_digest"):
                    raise AssertionError(f"loaded image id {loaded_id} != manifest.image_digest {manifest.get('image_digest')}")
                return {"loaded_image_id": loaded_id, "declared_version": manifest.get("version")}
            finally:
                temp.cleanup()

        scenario("build_integrity.artifact_sha256", check_sha256_matches_registration)
        scenario("build_integrity.filename_registration", check_filename_matches_registration)
        scenario("build_integrity.zip_not_corrupted", check_zip_not_corrupted)
        scenario("build_integrity.manifest_and_checksums", check_manifest_required_fields_and_checksums)
        scenario("build_integrity.filename_manifest_version_agree", check_filename_manifest_version_agree)
        scenario("build_integrity.internal_version_declarations_agree", check_internal_version_declarations_agree)
        scenario("build_integrity.runtime_image_identity_after_load", check_runtime_image_identity_after_load)

        status = "FAILED" if any(r["status"] == "FAILED" for r in results) else "PASSED"
        self.conn.execute("UPDATE qa_suite_runs SET status=?,finished_at=? WHERE id=?", (status, now(), suite_id))
        self.conn.commit()
        return {"suite_id": suite_id, "status": status, "scenarios": results}
