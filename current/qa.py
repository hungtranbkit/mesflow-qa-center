from __future__ import annotations

import argparse
import json
from pathlib import Path

from engine.replay import load_scenario
from engine.scenario_generator import ScenarioGenerator

MODES = ("SMOKE", "REGRESSION", "HUMAN_FLOW", "CHAOS", "SOAK", "FULL", "REPLAY")


def main() -> int:
    parser = argparse.ArgumentParser(prog="qa")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--mode", choices=MODES[:-1], default="SMOKE")
    run.add_argument("--seed", type=int, default=20260813)
    run.add_argument("--count", type=int, default=10)
    replay = sub.add_parser("replay")
    replay.add_argument("scenario")
    args = parser.parse_args()
    if args.command == "replay":
        scenario = load_scenario(Path(args.scenario))
        print(json.dumps(scenario.to_dict(), ensure_ascii=False, sort_keys=True))
        return 0
    scenarios = ScenarioGenerator(args.seed).generate_many(args.count)
    print(json.dumps({"mode": args.mode, "seed": args.seed, "scenarios": [x.to_dict() for x in scenarios]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
