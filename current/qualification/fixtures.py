from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


class FixtureError(RuntimeError):
    pass


class FixtureCatalog:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def load(self, version: str) -> dict[str, Any]:
        if not version or any(part in version for part in ("/", "\\", "..")):
            raise FixtureError("invalid fixture version")
        path = (self.root / f"{version}.json").resolve()
        if path.parent != self.root or not path.is_file():
            raise FixtureError(f"unknown fixture dataset: {version}")
        raw = path.read_bytes()
        data = json.loads(raw)
        if data.get("version") != version:
            raise FixtureError("fixture version does not match filename")
        for required in ("employees", "production_orders", "parts", "operations", "sessions", "repairs", "exceptions"):
            if required not in data or not isinstance(data[required], list):
                raise FixtureError(f"fixture is missing list: {required}")
        data["sha256"] = hashlib.sha256(raw).hexdigest()
        return data
