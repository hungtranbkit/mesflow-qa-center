from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Sequence

from .scenario_generator import Scenario


def save_reproduction(root: Path, bug_id: str, scenario: Scenario, expected: dict, actual: dict | None = None) -> Path:
    target = root / bug_id
    target.mkdir(parents=True, exist_ok=True)
    (target / "scenario.json").write_text(json.dumps(scenario.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (target / "expected.json").write_text(json.dumps(expected, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [f"# {bug_id}", "", f"- Scenario: `{scenario.scenario_id}`", f"- Seed: `{scenario.seed}`", f"- Fingerprint: `{scenario.fingerprint}`"]
    if actual is not None:
        lines += ["", "## Actual", "", "```json", json.dumps(actual, ensure_ascii=False, indent=2), "```"]
    (target / "reproduction.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def load_scenario(path: Path) -> Scenario:
    source = path / "scenario.json" if path.is_dir() else path
    return Scenario.from_dict(json.loads(source.read_text(encoding="utf-8")))


def shrink_actions(actions: Sequence[dict], reproduces: Callable[[list[dict]], bool]) -> list[dict]:
    """Deterministic delta-debugging reducer for a failing action sequence."""
    current = list(actions)
    granularity = 2
    while len(current) >= 2:
        size = max(1, len(current) // granularity)
        reduced = False
        for start in range(0, len(current), size):
            candidate = current[:start] + current[start + size:]
            if candidate and reproduces(candidate):
                current = candidate
                granularity = max(2, granularity - 1)
                reduced = True
                break
        if not reduced:
            if granularity >= len(current):
                break
            granularity = min(len(current), granularity * 2)
    return current
