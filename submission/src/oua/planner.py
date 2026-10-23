"""Step 2, "choose a goal": deterministic ranking of the goals a viewer is provably eligible for.

Order of preference (lifecycle stage first, then value, then how likely the agent can move the signal in-window):
  G04 trial_to_paid       a live trial is the most time-critical decision
  G01 first_play_24h      brand-new viewer who has not played anything
  G03 second_day_return   new viewer who has played: build the return habit
  G12 return_after_absence returning after 14+ days away
  G07 resume_series       an unfinished series is the strongest content hook we have
  G05 first_paid (intent) only when the viewer saw a paywall in the last 14 days
  G02 first_full_episode  has never finished an episode
  G06 reactivate_lapsed   recently lapsed subscriber
  G09 weekly_habit        active subscriber who rarely watches
  G08 try_new_release     active subscriber
  G05 first_paid          no purchase intent seen: base rate is ~0.4%, so it comes last
G10 and G11 are never chosen (not measurable). The model may review the choice but cannot change it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .facts import Facts
from .goals import EligibilityResult, GoalRegistry

ORDER = ["G04", "G01", "G03", "G12", "G07", "G05_intent", "G02", "G06", "G09", "G08", "G05"]
ALL_GOALS = ["G01", "G02", "G03", "G04", "G05", "G06", "G07", "G08", "G09", "G10", "G11", "G12"]


@dataclass
class GoalChoice:
    goal_id: str | None
    result: EligibilityResult | None
    candidates: list[str] = field(default_factory=list)
    considered: dict[str, list[str]] = field(default_factory=dict)  # goal -> reasons (eligible or not)
    why: str = ""


def choose_goal(registry: GoalRegistry, fs: Facts, now: datetime, avoid: set[str] | None = None) -> GoalChoice:
    avoid = avoid or set()
    results = {g: registry.eligibility(g, fs, now) for g in ALL_GOALS}
    considered = {g: (["eligible"] if r.eligible else ["ineligible"]) + r.reasons for g, r in results.items()}
    eligible = [g for g, r in results.items() if r.eligible]
    ranked: list[str] = []
    for key in ORDER:
        gid = "G05" if key == "G05_intent" else key
        if gid not in eligible or gid in ranked:
            continue
        if key == "G05_intent" and not fs.get("paywall_views_14d"):
            continue
        ranked.append(gid)
    skipped = [g for g in ranked if g in avoid]
    usable = [g for g in ranked if g not in avoid]
    if not usable:
        why = "no measurable goal the viewer is provably eligible for"
        if skipped:
            why += f" (skipped {skipped}: failed recently for this viewer)"
        return GoalChoice(None, None, ranked, considered, why)
    gid = usable[0]
    why = f"highest-priority eligible goal; candidates in order: {ranked}"
    if gid == "G05" and fs.get("paywall_views_14d"):
        why += f"; purchase intent: {fs.get('paywall_views_14d')} paywall view(s) in 14 days"
    if skipped:
        why += f"; skipped {skipped} (failed recently for this viewer)"
    return GoalChoice(gid, results[gid], ranked, considered, why)


def evidence_summary(fs: Facts) -> dict:
    """Structured, data-only summary for the model. No free text from the catalogue or from viewers."""
    keys = ["subscription_status", "signup_at", "has_played", "has_completed", "play_days_14d",
            "paywall_views_14d", "last_open_before", "recent_lapse", "dialect_home", "history_covers_lifetime"]
    return {k: fs.get(k) for k in keys} | {"unfinished_series": len(fs.get("unfinished_series") or [])}
