"""Demo scenarios: a slice of the real replay day plus hand-built cases that show one behaviour each.

`run_demo(send, pack_dir)` drives any transport: `send(method, path, body) -> dict`. The seed script sends over
HTTP; the dashboard's "Load demo data" button calls the same routes in-process. Every viewer id comes from the pack.
"""
from __future__ import annotations

import csv
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

UTC = timezone.utc
FAULT_TRIGGER = "202cccb8-8127-465f-b2d0-038cf533197e"  # model claims an active trial for U-01991


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def event(user, name, at: datetime, dialect="d1", session=None, props=None, buffered_s=0, clock_offset_s=0):
    occurred = at - timedelta(seconds=buffered_s) + timedelta(seconds=clock_offset_s)
    sent = at + timedelta(seconds=clock_offset_s)
    return {"event_id": str(uuid.uuid4()), "user_id": user, "event": name, "occurred_at": iso(occurred),
            "sent_at": iso(sent), "received_at": iso(at), "deliver_at": iso(at),
            "session_id": session or str(uuid.uuid4()), "app_dialect": dialect, "app_language": "hin",
            "platform": "android", "app_version": "6.1.4", "properties": props or {}}


def _read_jsonl(p: Path) -> list[dict]:
    with open(p, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def run_demo(send: Callable[[str, str, dict | None], dict], pack_dir: Path, replay_triggers: int = 60) -> dict:
    pack = Path(pack_dir)
    arms = {r["user_id"]: r["arm"] for r in csv.DictReader(open(pack / "replay/arms.csv", encoding="utf-8"))}
    profiles: dict[str, list] = {}
    for r in csv.DictReader(open(pack / "data/profiles_snapshot.csv", encoding="utf-8")):
        profiles.setdefault(r["user_id"], []).append(r)

    def agent_viewer(pred, skip=()):
        for u in sorted(arms):
            if arms[u] == "agent" and u not in skip and len(profiles.get(u, [])) == 1 and pred(profiles[u][0]):
                return u
        raise LookupError("no matching viewer")

    scenarios: list[dict] = []

    def show(label, d, expect):
        d = d if isinstance(d, dict) else {}
        goal = d.get("goal") if isinstance(d.get("goal"), dict) else {}
        scenarios.append({"scenario": label, "expect": expect, "viewer_id": d.get("viewer_id"),
                          "decision_id": d.get("decision_id") or d.get("original_decision_id"),
                          "status": d.get("status", d.get("error")), "action": d.get("action"),
                          "goal": goal.get("id"),
                          "reason": d.get("no_action_reason") or d.get("goal_reason") or d.get("note") or ""})

    # 1. A slice of the real replay day (triggers + live events, in delivery order).
    trig = _read_jsonl(pack / "replay/triggers.jsonl")[:replay_triggers]
    cutoff = trig[-1]["deliver_at"]
    live = [e for e in _read_jsonl(pack / "replay/live_events.jsonl") if e["deliver_at"] <= cutoff]
    stream = sorted([("t", x) for x in trig] + [("e", x) for x in live], key=lambda kv: kv[1]["deliver_at"])
    counts: dict[str, int] = {}
    for kind, x in stream:
        r = send("POST", "/v1/triggers" if kind == "t" else "/v1/events", x) or {}
        if kind == "t":
            counts[r.get("status", "?")] = counts.get(r.get("status", "?"), 0) + 1

    # 2. Hand-built scenarios on 2026-10-01 (IST afternoon), one behaviour each.
    t = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    used: set[str] = set()
    u1 = agent_viewer(lambda p: p["subscription_status"] == "never" and p["preferred_dialect"] == "d1")
    used.add(u1)
    first = event(u1, "app_open", t, "d1", session="demo-s1")
    show("Normal app open", send("POST", "/v1/triggers", first), "home page published with a goal")
    show("Same trigger delivered again", send("POST", "/v1/triggers", first), "ignored as a duplicate")
    show("Login in the same session", send("POST", "/v1/triggers",
                                           event(u1, "login_success", t + timedelta(seconds=3), "d1", "demo-s1")),
         "no action: already acted for this session")

    u2 = agent_viewer(lambda p: p["preferred_dialect"] == "d1", used)
    used.add(u2)
    show("App open buffered 2 days offline", send("POST", "/v1/triggers", event(
        u2, "app_open", t + timedelta(minutes=1), "d1", buffered_s=2 * 86400)), "no action and no goal: stale")
    show("Phone clock wrong by 9 hours", send("POST", "/v1/triggers", event(
        u2, "app_open", t + timedelta(minutes=2), "d1", clock_offset_s=-9 * 3600)), "still fresh: published")

    u3 = agent_viewer(lambda p: p["preferred_dialect"] == "d1", used)
    used.add(u3)
    show("App d2 / profile d1 / campaign d5", send("POST", "/v1/triggers", event(
        u3, "app_open", t + timedelta(minutes=3), "d2", props={"deeplink": "ott://c/cmp032/d5/title/T1901"})),
        "page in d2 (app setting); others listed as conflicts")
    u4 = agent_viewer(lambda p: True, used)
    used.add(u4)
    show("App set to unsupported dialect d7", send("POST", "/v1/triggers", event(
        u4, "app_open", t + timedelta(minutes=4), "d7")), "no page: that dialect does not exist")

    hold = next(u for u in sorted(arms) if arms[u] == "holdout" and profiles.get(u))
    show("Holdout viewer", send("POST", "/v1/triggers", event(
        hold, "app_open", t + timedelta(minutes=5), profiles[hold][0]["preferred_dialect"])),
        "no action of any kind; shadow goal only")

    for u in sorted(arms):
        if u in used or arms[u] != "agent" or len(profiles.get(u, [])) != 1 or \
                profiles[u][0]["subscription_status"] != "never":
            continue
        d = profiles[u][0]["preferred_dialect"]
        used.add(u)
        r = send("POST", "/v1/triggers", event(u, "app_open", t + timedelta(minutes=6), d, session="demo-buy")) or {}
        if (r.get("goal") or {}).get("id") == "G05":
            show("Never-paid viewer gets goal G05", r, "home page, goal: first paid plan")
            buy = event(u, "purchase_completed", t + timedelta(minutes=8), d, session="demo-buy",
                        props={"plan_id": "P_MONTH", "price_inr": 149, "is_trial": False})
            show("Purchase 2 minutes later", (send("POST", "/v1/events", buy) or {}) | {"viewer_id": u, "status": "PLAN_REVISED"},
                 "plan closed, follow-up cancelled")
            show("Next visit after the purchase", send("POST", "/v1/triggers", event(
                u, "app_open", t + timedelta(hours=3), d)), "new goal for a paying viewer")
            break

    fault = next(x for x in _read_jsonl(pack / "replay/triggers.jsonl") if x["event_id"] == FAULT_TRIGGER)
    show("Model claims a trial with no evidence", send("POST", "/v1/triggers", fault),
         "claim rejected; deterministic goal used")

    send("POST", "/v1/control", {"at": iso(t + timedelta(minutes=30)), "type": "dependency_unavailable",
                                 "dependency": "model_provider", "until": iso(t + timedelta(minutes=40))})
    for k, u5 in enumerate(u for u in sorted(arms) if arms[u] == "agent" and u not in used and u != "U-01991"
                           and len(profiles.get(u, [])) == 1):
        r = send("POST", "/v1/triggers", event(u5, "app_open", t + timedelta(minutes=31, seconds=k),
                                               profiles[u5][0]["preferred_dialect"])) or {}
        if r.get("model"):
            show("Decision while the model is down", dict(
                r, goal_reason=f"model call failed ({r['model'][0]['error']}); deterministic decision used"),
                 "published anyway without the model")
            break
        if k > 40:
            break

    # 3. Run every follow-up nudge that is due by the end of 2 October.
    steps = send("POST", "/v1/plan-steps/run?now=2026-10-02T18:00:00Z", None) or []
    sent = [s for s in steps if s and s.get("status") == "PUBLISHED"]
    return {"replay_slice": {"triggers": len(trig), "live_events": len(live), "statuses": counts},
            "scenarios": scenarios,
            "nudges": {"due": len(steps), "sent": len(sent),
                       "channels": sorted({s["action"] for s in sent})}}
