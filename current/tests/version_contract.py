from __future__ import annotations

import ast
import re
from pathlib import Path


def source_version(root: Path) -> str:
    version = (root / "VERSION").read_text(encoding="utf-8").strip()
    if not version:
        raise AssertionError("VERSION must not be empty")
    return version


def application_version(root: Path) -> str:
    tree = ast.parse((root / "agent.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "APP_VERSION" for target in node.targets):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                return node.value.value
    raise AssertionError("agent.py must declare a literal APP_VERSION")


def installer_version(root: Path) -> str:
    match = re.search(r'^APP_VERSION="([^"]+)"$', (root / "install.sh").read_text(encoding="utf-8"), re.MULTILINE)
    if not match:
        raise AssertionError("install.sh must declare APP_VERSION")
    return match.group(1)


def assert_version_contract(root: Path) -> str:
    expected = source_version(root)
    assert application_version(root) == expected
    assert installer_version(root) == expected
    return expected
