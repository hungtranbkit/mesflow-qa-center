from __future__ import annotations

import argparse
import json
from pathlib import Path

from .browser import run_browser_suite


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--target-url", required=True)
    parser.add_argument("--evidence-root", required=True, type=Path)
    args = parser.parse_args()
    result = run_browser_suite(args.run_id, args.target_url, args.evidence_root)
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
