"""Step 6, "close the loop": judge every goal instance from observed signals and feed lessons back to the viewer.

Two separate questions are answered for each goal:
  rule_met      did the success signal (current rule version) occur inside the window? Used for impact (ITT), the
                same way for agent and holdout goals.
  verdict       for agent-arm goals: succeeded only when rule_met AND the signal is attributable to our action
                (an exposure to our page/message preceded it, and a purchase was not already in flight).
                rule_met without attribution -> inconclusive. No signal by the deadline -> failed. Deadline beyond
                the observation horizon -> inconclusive.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from .agent import AgentSystem
from .events import normalize, read_jsonl
from .timeutil import IST, iso, parse

PURCHASE_GOALS = {"G04", "G05", "G06"}


def load_outcome_events(sim_dir: Path) -> dict[str, list[dict]]:
    by_user: dict[str, list[dict]] = defaultdict(list)
    seen = set()
    for e in read_jsonl(Path(sim_dir) / "outcome_events.jsonl"):
        if e["event_id"] in seen:
            continue
        seen.add(e["event_id"])
        by_user[e["user_id"]].append(normalize(e))
    for v in by_user.values():
        v.sort(key=lambda n: n["_received"])
    return by_user


def judge_all(system: AgentSystem, sim_dir: Path, out_path: Path, horizon_end: datetime | None = None) -> list[dict]:
    events = load_outcome_events(sim_dir)
    horizon_end = horizon_end or datetime(2026, 10, 16, tzinfo=IST)  # replay day + 14 simulated days
    actions_by_user: dict[str, set] = defaultdict(set)
    for a in system.state.actions():
        actions_by_user[a["user_id"]].add(a["action_id"])
    s = system.s
    outcomes = []
    lessons: dict[str, list[dict]] = defaultdict(list)
    for g in system.state.goals():
        if g["status"] == "superseded":
            outcomes.append(_row(g, False, None, "superseded", "goal premise changed before the deadline "
                                 "(e.g. a purchase); not judged", None, []))
            continue
        evs = events.get(g["user_id"], [])
        ev, ver = system.registry.success_event(g, evs, system.pack)
        start, deadline = parse(g["started_at"]), parse(g["deadline"])
        exposures = []
        if g["arm"] == "agent":
            mine = actions_by_user[g["user_id"]]
            for e in evs:
                p = e.get("properties") or {}
                if e["_received"] < start:
                    continue
                if e["event"] == "home_page_shown" and p.get("personalised") and p.get("page_action_id") in mine:
                    exposures.append(e)
                elif e["event"] in ("message_clicked", "message_delivered") and p.get("action_id") in mine:
                    exposures.append(e)
        attribution = None
        if ev is None:
            if deadline > horizon_end:
                verdict, why = "inconclusive", "deadline is beyond the observation horizon and no signal yet"
            else:
                verdict, why = "failed", f"no {g['success_signal']['event']} matching rule {ver} before the deadline"
        elif g["arm"] != "agent":
            verdict, why = "succeeded", "holdout goal (no action): rule met organically"
            attribution = "n/a (holdout)"
        else:
            before = [x for x in exposures if x["_received"] <= ev["_received"]]
            lag = (ev["_received"] - start).total_seconds()
            if g["goal_id"] in PURCHASE_GOALS and lag < s.purchase_min_attribution_lag_s:
                verdict, attribution = "inconclusive", "in_flight"
                why = (f"purchase {int(lag)}s after the decision: the purchase flow was already under way, so it "
                       "cannot be credited to the agent")
            elif not before:
                verdict, attribution = "inconclusive", "no_prior_exposure"
                why = "signal happened, but the viewer had not seen any agent page or message before it"
            else:
                verdict, attribution = "succeeded", f"exposed_before_signal:{before[-1]['event']}"
                why = f"signal {ev['event']} after exposure to agent action ({before[-1]['event']})"
        outcomes.append(_row(g, ev is not None, ev, verdict, why, attribution, exposures, ver))
        if g["arm"] == "agent":
            lessons[g["user_id"]].append(_lesson(g, verdict, attribution, exposures, ev))
    # Delivery facts learned from the simulator (message failures, opt-outs).
    for u, evs in events.items():
        for e in evs:
            p = e.get("properties") or {}
            if e["event"] == "message_failed" and p.get("action_id") in actions_by_user[u]:
                lessons[u].append({"lesson": f"{p.get('channel')} failed: {p.get('reason')}",
                                   "effect": "disable this channel for the viewer" if p.get("reason") == "token_invalid"
                                   else "re-check consent", "source": e["event_id"]})
            elif e["event"] == "notification_opt_out" and actions_by_user[u]:
                lessons[u].append({"lesson": f"opted out of {p.get('channel')}", "effect": "consent revoked",
                                   "source": e["event_id"]})
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        for o in outcomes:
            f.write(json.dumps(o, default=str) + "\n")
    _feed_back(system, outcomes, lessons)
    return outcomes


def _row(g, met, ev, verdict, why, attribution, exposures, ver=None) -> dict:
    return {"goal_instance_id": g["goal_instance_id"], "viewer_id": g["user_id"], "arm": g["arm"],
            "goal_id": g["goal_id"], "decision_id": g["decision_id"], "started_at": g["started_at"],
            "deadline": g["deadline"], "rule_version_judged": ver or g["rule_version"],
            "rule_version_at_start": g.get("history", [{}])[0].get("from", g["rule_version"])
            if any(h.get("event") == "rule_corrected" for h in g.get("history", [])) else g["rule_version"],
            "rule_met": met, "success_event": f"event:{ev['event_id']}" if ev else None,
            "success_at": ev["received_at"] if ev else None, "verdict": verdict, "attribution": attribution,
            "exposures": len(exposures), "reason": why, "judged_at": iso(datetime(2026, 10, 16, tzinfo=IST))}


def _lesson(g, verdict, attribution, exposures, ev) -> dict:
    if verdict == "succeeded":
        text = f"{g['goal_id']} reached after exposure; the same lever can be reused"
    elif verdict == "failed" and exposures:
        text = f"{g['goal_id']} failed despite {len(exposures)} exposure(s); avoid this goal for 7 days, try another lever"
    elif verdict == "failed":
        text = f"{g['goal_id']} failed and the viewer never saw the intervention (delivery, not content, was the gap)"
    else:
        text = f"{g['goal_id']} inconclusive ({attribution or 'no signal yet'})"
    return {"goal_instance_id": g["goal_instance_id"], "goal_id": g["goal_id"], "verdict": verdict,
            "judged_at": "2026-10-16T00:00:00.000Z", "lesson": text}


def _feed_back(system: AgentSystem, outcomes: list[dict], lessons: dict[str, list[dict]]) -> None:
    st = system.state
    by_goal = {o["goal_instance_id"]: o for o in outcomes}
    with st.tx():
        for g in st.goals():
            o = by_goal.get(g["goal_instance_id"])
            if o and g["status"] != "superseded":
                g["status"] = o["verdict"]
                g["verdict"] = {k: o[k] for k in ("verdict", "attribution", "reason", "success_event", "rule_met")}
                st.put_goal(g)
        for u, ls in lessons.items():
            doc, ver = st.load_viewer(u)
            if not ver:
                continue
            doc["learned"] = [x for x in doc["learned"] if "goal_instance_id" not in x] + ls
            if any("token_invalid" in x["lesson"] for x in ls):
                doc["channel_state"] = {"push": "disabled: token_invalid"}
            ag = doc.get("active_goal")
            if ag and ag["goal_instance_id"] in by_goal:
                doc["goal_history"].append({"goal_instance_id": ag["goal_instance_id"], "goal_id": ag["goal_id"],
                                            "status": by_goal[ag["goal_instance_id"]]["verdict"]})
                doc["active_goal"] = None
            st.save_viewer(u, doc, ver)
