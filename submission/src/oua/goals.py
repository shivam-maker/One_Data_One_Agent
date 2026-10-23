"""Goal catalogue: deterministic eligibility from facts, versioned success rules, and measurability.

Eligibility is computed only from facts that carry evidence. A model may *review* a choice, but its claims are never
inputs here.

Decisions about the catalogue itself (documented in ARCHITECTURE.md):
  * G10 meaningful_engagement_7d is never chosen: its success signal is null ("engages meaningfully").
  * G11 tv_activation_14d is never chosen: tv_app_open does not exist in the event stream, so it cannot be judged.
  * G06 "recently lapsed" is underspecified: we define it as a lapse within Settings.recent_lapse_days.
  * G07 has no structured rule: we derive it from episodes.csv and the viewer's play/completion history.
  * Success rules are versioned. A goal_rule_corrected control event adds a version; open goals of that goal_id are
    re-pointed to the new version (only those viewers are touched).
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .facts import Facts
from .timeutil import IST, iso, ist_day, parse

KNOWN_EVENTS = {
    "signup_completed", "login_success", "app_open", "app_dialect_changed", "home_viewed", "title_viewed", "search",
    "play_started", "play_progress", "episode_completed", "paywall_viewed", "purchase_completed",
    "subscription_renewed", "subscription_lapsed", "message_delivered", "message_clicked", "notification_opt_out",
    "home_page_shown", "message_failed",
}


@dataclass
class EligibilityResult:
    goal_id: str
    eligible: bool
    reasons: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    certainty: float = 1.0  # < 1 when eligibility rests on a documented assumption


class GoalRegistry:
    def __init__(self, goals_doc: dict):
        self.version_doc = goals_doc.get("version")
        self.goals: dict[str, dict] = {g["goal_id"]: g for g in goals_doc["goals"]}
        # goal_id -> list of success rule versions [{version, success, effective_at, note}]
        self.success_versions: dict[str, list[dict]] = {
            gid: [{"version": f"{gid}-r1", "success": copy.deepcopy(g["success"]), "effective_at": None,
                   "note": "as published in goals.json"}]
            for gid, g in self.goals.items()
        }

    # ---- rule versions -------------------------------------------------------------------------------------
    def current_rule(self, goal_id: str) -> dict:
        return self.success_versions[goal_id][-1]

    def correct(self, goal_id: str, new_success: dict, at: datetime, note: str) -> dict:
        prev = self.current_rule(goal_id)
        merged = copy.deepcopy(prev["success"])
        merged.update({k: v for k, v in new_success.items() if v is not None})
        v = {"version": f"{goal_id}-r{len(self.success_versions[goal_id]) + 1}", "success": merged,
             "effective_at": iso(at), "note": note}
        self.success_versions[goal_id].append(v)
        return v

    def measurable(self, goal_id: str) -> tuple[bool, str]:
        s = self.current_rule(goal_id)["success"]
        if not s.get("event"):
            return False, "success signal is undefined (event is null)"
        if s["event"] not in KNOWN_EVENTS:
            return False, f"success event '{s['event']}' never appears in the event stream"
        return True, "ok"

    def window(self, goal_id: str) -> timedelta:
        return timedelta(hours=self.current_rule(goal_id)["success"]["window_hours"])

    # ---- eligibility ---------------------------------------------------------------------------------------
    def eligibility(self, goal_id: str, fs: Facts, now: datetime) -> EligibilityResult:
        r = EligibilityResult(goal_id, False)
        ok, why = self.measurable(goal_id)
        if not ok:
            r.reasons.append(f"not measurable: {why}")
            return r
        rule_ref = f"goal_rule:{goal_id}:eligibility"
        signup = parse(fs.get("signup_at"))
        status = fs.get("subscription_status")
        st_fact = fs.fact("subscription_status")
        status_refs = st_fact.refs if st_fact else []

        def since_signup_days():
            return (now - signup).total_seconds() / 86400 if signup else None

        if goal_id == "G01":
            d = since_signup_days()
            if d is None:
                r.reasons.append("signup time unknown")
            elif d > 7:
                r.reasons.append(f"signed up {d:.1f} days ago (> 7)")
            elif fs.get("has_played"):
                r.reasons.append("already started a play")
            else:
                r.eligible = True
                r.evidence = [rule_ref, *fs.fact("signup_at").refs]
                r.reasons.append(f"signed up {d:.1f} days ago and no play_started in full history")
        elif goal_id == "G02":
            if fs.get("has_completed"):
                r.reasons.append("already completed an episode")
            elif fs.get("history_covers_lifetime"):
                r.eligible = True
                r.evidence = [rule_ref, *fs.fact("signup_at").refs]
                r.reasons.append("no episode_completed since signup (history covers the whole lifetime)")
            elif fs.get("has_played"):
                r.eligible = True
                r.certainty = 0.8
                r.evidence = [rule_ref, *fs.fact("has_played").refs]
                r.assumptions.append("no episode was completed before the history window (2026-09-01)")
                r.reasons.append("plays in the history window but no completion")
            else:
                r.reasons.append("history window does not cover signup and shows no plays; cannot show 'never'")
        elif goal_id == "G03":
            d = since_signup_days()
            if d is None:
                r.reasons.append("signup time unknown")
            elif d > 2:
                r.reasons.append(f"signed up {d:.1f} days ago (> 2)")
            else:
                r.eligible = True
                r.evidence = [rule_ref, *fs.fact("signup_at").refs]
                r.reasons.append(f"signed up {d:.1f} days ago")
        elif goal_id in ("G04", "G05", "G06", "G08", "G09"):
            allowed = self.goals[goal_id]["eligibility"].get("subscription_status", [])
            if status is None:
                r.reasons.append("subscription status unknown")
            elif status not in allowed:
                r.reasons.append(f"subscription status is {status}, rule needs {allowed}")
            else:
                r.eligible = True
                r.evidence = [rule_ref, *status_refs]
                r.reasons.append(f"subscription status {status} ({st_fact.source})")
                if st_fact.status == "conflict_resolved":
                    r.certainty = 0.75
                    r.assumptions.append(f"status conflict resolved: {st_fact.resolution}")
            if r.eligible and goal_id == "G05" and fs.get("ever_paid"):
                r.eligible = False
                r.reasons.append("evidence of an earlier paid plan")
            if r.eligible and goal_id == "G06":
                if not fs.get("recent_lapse"):
                    r.eligible = False
                    r.reasons.append("no lapse within the 'recent' window")
                else:
                    r.evidence += fs.fact("recent_lapse").refs
                    r.assumptions.append(fs.fact("recent_lapse").resolution)
            if r.eligible and goal_id == "G09" and fs.get("play_days_14d") > 1:
                r.eligible = False
                r.reasons.append(f"played on {fs.get('play_days_14d')} days in the last 14 (rule: at most 1)")
        elif goal_id == "G07":
            cands = [u for u in fs.get("unfinished_series") or [] if u["days_since"] >= 3]
            if not cands:
                r.reasons.append("no unfinished show/micro-drama idle for 3+ days")
            else:
                r.eligible = True
                r.evidence = [rule_ref, cands[0]["ref"], f"catalogue:{cands[0]['title_id']}"]
                r.reasons.append(f"unfinished {cands[0]['slug']} idle {cands[0]['days_since']} days")
        elif goal_id == "G12":
            last = parse(fs.get("last_open_before"))
            d = since_signup_days()
            if d is not None and d < 14:
                r.reasons.append("account younger than 14 days: absence is not defined")
            elif last and now - last < timedelta(days=14):
                r.reasons.append(f"opened the app {((now - last).total_seconds() / 86400):.1f} days ago")
            elif last is None and not fs.get("history_covers_lifetime"):
                r.eligible = True
                r.certainty = 0.85
                r.evidence = [rule_ref]
                r.assumptions.append("no app_open anywhere in the 30-day history implies none in the last 14 days")
                r.reasons.append("no earlier app_open in the history window")
            else:
                r.eligible = True
                r.evidence = [rule_ref, *fs.fact("last_open_before").refs]
                r.reasons.append("last app_open more than 14 days before the trigger")
        else:
            r.reasons.append("goal never chosen by policy")
        return r

    # ---- success -------------------------------------------------------------------------------------------
    def success_event(self, goal: dict, events: list[dict], pack=None) -> tuple[dict | None, str]:
        """First event that satisfies the goal's *current* success rule inside its window.

        events must be normalised and sorted by received time. Returns (event, rule_version)."""
        gid = goal["goal_id"]
        rule = self.current_rule(gid)
        s = rule["success"]
        start, deadline = parse(goal["started_at"]), parse(goal["deadline"])
        start_day = ist_day(start)
        days_seen = set()
        for e in events:
            t = e["_received"]
            if t < start or t > deadline or e["event"] != s["event"]:
                continue
            props = e.get("properties") or {}
            if gid in ("G04", "G05") and props.get("is_trial"):
                continue
            if gid == "G03" and ist_day(t) <= start_day:
                continue
            if gid == "G07" and pack is not None:
                ti = pack.titles.get(props.get("title_id"))
                if not ti or ti.slug != goal.get("target_slug"):
                    continue
            if gid == "G08" and pack is not None:
                ti = pack.titles.get(props.get("title_id"))
                rd = parse(ti.release_date + "T00:00:00Z") if ti else None
                if not rd or not (start - timedelta(days=30) <= rd <= start):
                    continue
            if gid == "G09":
                days_seen.add(ist_day(t))
                if len(days_seen) < 3:
                    continue
            return e, rule["version"]
        return None, rule["version"]
