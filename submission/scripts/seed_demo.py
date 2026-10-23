"""Seed a running API with demo traffic so the endpoints and the dashboard have something to show.

    python -m oua serve --reset               # terminal 1 (from submission/src), leave running
    python scripts/seed_demo.py               # terminal 2 (from submission/)

The dashboard at http://127.0.0.1:8000/ has a "Load demo data" button that does the same thing.
Standard library only; the scenarios live in src/oua/demo.py.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "src"))

from oua.demo import run_demo  # noqa: E402


def make_send(base: str):
    def send(method: str, path: str, body: dict | None = None):
        req = urllib.request.Request(base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read() or "null")
        except urllib.error.HTTPError as e:
            return {"error": e.code, "detail": e.read().decode()[:200]}
        except urllib.error.URLError as e:
            if path == "/health":
                return {"error": "unreachable", "detail": str(e.reason)}
            raise
    return send


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://127.0.0.1:8000")
    ap.add_argument("--pack", default=os.environ.get("OUA_PACK", str(HERE.parent / "one_user_one_agent_pack")))
    ap.add_argument("--replay-triggers", type=int, default=60, help="how many real replay triggers to send first")
    a = ap.parse_args()
    send = make_send(a.api)

    health = send("GET", "/health")
    if not isinstance(health, dict) or not health.get("ok"):
        sys.exit(f"The API is not running at {a.api} ({health.get('detail') if isinstance(health, dict) else health}).\n"
                 "Start it first in ANOTHER terminal and leave it running:\n"
                 '    cd "...\\stage_project\\submission\\src"\n'
                 "    python -m oua serve --reset\n"
                 "Wait for 'Uvicorn running on http://127.0.0.1:8000', then run this script again.")

    r = run_demo(send, Path(a.pack), a.replay_triggers)
    s = r["replay_slice"]
    print(f"real replay slice: {s['triggers']} triggers + {s['live_events']} live events -> {s['statuses']}")
    n = r["nudges"]
    print(f"follow-up nudges: {n['due']} due, {n['sent']} sent ({', '.join(n['channels']) or 'none'}), "
          "rest cancelled with a recorded reason")
    w = [36, 8, 9, 18, 10, 5]
    print("\n" + "  ".join(h.ljust(k) for h, k in zip(["scenario", "viewer", "decision", "status", "action", "goal"], w))
          + "  reason")
    print("-" * 150)
    for x in r["scenarios"]:
        cells = [x["scenario"], x["viewer_id"], x["decision_id"], x["status"], x["action"], x["goal"]]
        print("  ".join(str(c or "-").ljust(k)[:k] for c, k in zip(cells, w)) + "  " + (x["reason"] or "")[:70])
    print(f"\nOpen the dashboard: {a.api}/")


if __name__ == "__main__":
    main()
