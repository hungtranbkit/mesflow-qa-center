#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import agent  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="MESFlow QA Center demo database automation")
    ap.add_argument("action", choices=["preview", "prepare", "reset"])
    ap.add_argument("--confirm", default="")
    ns = ap.parse_args()
    cfg = agent.load_config()
    url = agent._cleanup_database_url(cfg)
    if ns.action == "preview":
        out = agent.demo_database_preview(url)
    elif ns.action == "prepare":
        out = agent.demo_database_prepare_execute(url, ns.confirm)
    else:
        out = agent.demo_database_reset_execute(url, ns.confirm)
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0 if out.get("ok") else 2

if __name__ == "__main__":
    raise SystemExit(main())
