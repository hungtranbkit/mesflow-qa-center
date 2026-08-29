from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from pathlib import Path
from typing import Any

from .store import connect, now


class EvidenceStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _digest(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def add_file(self, run_id: str, source: Path, *, kind: str,
                 suite_run_id: str | None = None, scenario_run_id: str | None = None,
                 metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        source = source.resolve(strict=True)
        destination_dir = self.root / run_id
        destination_dir.mkdir(parents=True, exist_ok=True)
        evidence_id = f"ev-{uuid.uuid4().hex}"
        destination = destination_dir / f"{evidence_id}-{source.name}"
        shutil.copy2(source, destination)
        sha256 = self._digest(destination)
        relative = destination.relative_to(self.root).as_posix()
        conn = connect()
        conn.execute(
            """INSERT INTO qa_evidence(id,qualification_run_id,suite_run_id,scenario_run_id,kind,
               filename,sha256,size_bytes,relative_path,metadata_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (evidence_id, run_id, suite_run_id, scenario_run_id, kind, source.name, sha256,
             destination.stat().st_size, relative, json.dumps(metadata or {}, sort_keys=True), now()),
        )
        conn.commit()
        return {"id": evidence_id, "kind": kind, "filename": source.name, "sha256": sha256,
                "size_bytes": destination.stat().st_size, "relative_path": relative}

    def write_json(self, run_id: str, name: str, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)
        temp = self.root / run_id / f"source-{uuid.uuid4().hex}-{safe}"
        temp.parent.mkdir(parents=True, exist_ok=True)
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        try:
            return self.add_file(run_id, temp, **kwargs)
        finally:
            temp.unlink(missing_ok=True)
