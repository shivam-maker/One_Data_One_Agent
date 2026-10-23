"""Step 7, "prove impact": intention-to-treat comparison against the randomized holdout, plus the misleading
comparisons we deliberately do not use, and a re-analysis of the earlier pilot.

Unit: viewer. Population: every viewer with at least one replay-day trigger, analysed by randomized arm regardless
of whether the agent acted (intention to treat).
Pre-declared primary metric: share of viewers whose first committed goal met its success rule (goals are committed
by the same deterministic policy in both arms, so the goal mix is comparable).
"""
from __future__ import annotations

import csv
import json
import math
from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path

from .agent import AgentSystem
from .judge import load_outcome_events
from .timeutil import parse


def two_prop(a_succ, a_n, b_succ, b_n) -> dict:
    pa, pb = (a_succ / a_n if a_n else 0.0), (b_succ / b_n if b_n else 0.0)
    diff = pa - pb
    se = math.sqrt(pa * (1 - pa) / a_n + pb * (1 - pb) / b_n) if a_n and b_n else float("nan")
    pooled = (a_succ + b_succ) / (a_n + b_n) if a_n + b_n else 0
    se0 = math.sqrt(pooled * (1 - pooled) * (1 / a_n + 1 / b_n)) if a_n and b_n and 0 < pooled < 1 else float("nan")
    z = diff / se0 if se0 and not math.isnan(se0) else float("nan")
    p = math.erfc(abs(z) / math.sqrt(2)) if not math.isnan(z) else float("nan")
    return {"a": f"{a_succ}/{a_n}", "b": f"{b_succ}/{b_n}", "rate_a": round(pa, 4), "rate_b": round(pb, 4),
            "diff_pp": round(100 * diff, 2), "ci95_pp": [round(100 * (diff - 1.96 * se), 2),
                                                          round(100 * (diff + 1.96 * se), 2)],
            "p_value": round(p, 4) if not math.isnan(p) else None}


def analyse(system: AgentSystem, sim_dir: Path, outcomes: list[dict], out_md: Path) -> dict:
    pack = system.pack
    triggers_by_user: dict[str, str] = {}
    fresh_users: set[str] = set()
    for d in system.state.decisions():
        if d["kind"] == "trigger":
            u = d["viewer_id"]
            if d["trigger"]["event_age_s"] <= system.s.stale_trigger_s:
                fresh_users.add(u)
            t = d["trigger"]["deliver_at"]
            if u not in triggers_by_user or t < triggers_by_user[u]:
                triggers_by_user[u] = t
    arms = {u: pack.arms.get(u) for u in triggers_by_user}
    arm_users = {a: [u for u, x in arms.items() if x == a] for a in ("agent", "holdout")}
    events = load_outcome_events(sim_dir)

    # ---- primary: first goal per viewer, rule_met (ITT over viewers who got a goal) -------------------------
    first_goal: dict[str, dict] = {}
    for o in sorted(outcomes, key=lambda o: o["goal_instance_id"]):
        first_goal.setdefault(o["viewer_id"], o)
    prim = {}
    for a in ("agent", "holdout"):
        gs = [first_goal[u] for u in arm_users[a] if u in first_goal and first_goal[u]["verdict"] != "superseded"]
        prim[a] = (sum(1 for g in gs if g["rule_met"]), len(gs))
    primary = two_prop(*prim["agent"], *prim["holdout"])
    goal_assignment_rate = {a: round(prim[a][1] / max(1, len(arm_users[a])), 4) for a in prim}
    stale_only = {a: sum(1 for u in arm_users[a] if u not in fresh_users) for a in arm_users}

    per_goal = {}
    for gid in sorted({o["goal_id"] for o in first_goal.values()}):
        r = {}
        for a in ("agent", "holdout"):
            gs = [first_goal[u] for u in arm_users[a] if u in first_goal and first_goal[u]["goal_id"] == gid
                  and first_goal[u]["verdict"] != "superseded"]
            r[a] = (sum(1 for g in gs if g["rule_met"]), len(gs))
        per_goal[gid] = two_prop(*r["agent"], *r["holdout"])

    # ---- secondary: goal-agnostic outcomes over every triggered viewer ---------------------------------------
    def metric(u, kind):
        t0 = parse(triggers_by_user[u])
        for e in events.get(u, []):
            dt = e["_received"] - t0
            if dt < timedelta(0):
                continue
            if kind == "play_3d" and e["event"] == "play_started" and dt <= timedelta(days=3):
                return True
            if kind == "complete_3d" and e["event"] == "episode_completed" and dt <= timedelta(days=3):
                return True
            if kind == "purchase_7d" and e["event"] == "purchase_completed" and dt <= timedelta(days=7) and \
                    not (e.get("properties") or {}).get("is_trial"):
                return True
            if kind == "return_day2_7" and e["event"] == "app_open" and timedelta(days=1) <= dt <= timedelta(days=7):
                return True
        return False

    secondary = {}
    for kind in ("play_3d", "complete_3d", "return_day2_7", "purchase_7d"):
        c = {a: sum(1 for u in arm_users[a] if metric(u, kind)) for a in arm_users}
        secondary[kind] = two_prop(c["agent"], len(arm_users["agent"]), c["holdout"], len(arm_users["holdout"]))

    # ---- the misleading comparisons ----------------------------------------------------------------------------
    served = {u for u in arm_users["agent"] if any(
        e["event"] == "home_page_shown" and (e.get("properties") or {}).get("personalised") for e in events.get(u, []))}
    others = [u for u in triggers_by_user if u not in served]
    s_succ = sum(1 for u in served if metric(u, "play_3d"))
    o_succ = sum(1 for u in others if metric(u, "play_3d"))
    naive_pilot_style = two_prop(s_succ, len(served), o_succ, len(others))
    unserved_agent = [u for u in arm_users["agent"] if u not in served]
    naive_within = two_prop(s_succ, len(served), sum(1 for u in unserved_agent if metric(u, "play_3d")),
                            len(unserved_agent))

    # ---- balance check on pre-period behaviour (randomization sanity) -----------------------------------------
    balance = {}
    for a, us in arm_users.items():
        hist_plays = sum(1 for u in us for e in system.events.history(u, parse("2026-09-30T18:30:00Z"))
                         if e["event"] == "play_started")
        statuses = Counter()
        for u in us:
            rows = pack.profiles.get(u)
            statuses[rows[-1]["subscription_status"] if rows else "no_profile"] += 1
        balance[a] = {"viewers": len(us), "pre_period_plays_per_viewer": round(hist_plays / max(1, len(us)), 2),
                      "status_mix": dict(statuses)}

    pilot = pilot_reanalysis(pack.root)
    result = {"population": {a: len(v) for a, v in arm_users.items()}, "goal_assignment_rate": goal_assignment_rate,
              "stale_only_viewers": stale_only,
              "primary_goal_success_itt": primary, "per_goal": per_goal, "secondary_itt": secondary,
              "naive_served_vs_everyone_else_play3d": naive_pilot_style,
              "naive_served_vs_unserved_in_agent_arm_play3d": naive_within, "served_viewers": len(served),
              "balance": balance, "pilot": pilot,
              "verdicts_agent_arm": dict(Counter(o["verdict"] for o in outcomes if o["arm"] == "agent")),
              "attribution_agent_arm": dict(Counter(o["attribution"] for o in outcomes if o["arm"] == "agent"
                                                    and o["rule_met"]))}
    (out_md.parent / "impact.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    out_md.write_text(render(result), encoding="utf-8")
    return result


def pilot_reanalysis(pack_root: Path) -> dict:
    rows = list(csv.DictReader(open(Path(pack_root) / "pilot/pilot_outcomes.csv", encoding="utf-8")))
    g = defaultdict(lambda: [0, 0])
    for r in rows:
        k = (r["arm"], r["served"] == "true")
        g[k][0] += r["succeeded"] == "true"
        g[k][1] += 1
    t = [g[("treatment", True)][0] + g[("treatment", False)][0], g[("treatment", True)][1] + g[("treatment", False)][1]]
    h = g[("holdout", False)]
    others = [g[("treatment", False)][0] + h[0], g[("treatment", False)][1] + h[1]]
    return {"report_claim_served_vs_others": two_prop(*g[("treatment", True)], *others),
            "itt_treatment_vs_holdout": two_prop(*t, *h),
            "treatment_unserved_vs_holdout": two_prop(*g[("treatment", False)], *h)}


def _fmt(r: dict) -> str:
    return (f"{100 * r['rate_a']:.1f}% ({r['a']}) vs {100 * r['rate_b']:.1f}% ({r['b']}): "
            f"**{r['diff_pp']:+.1f} pp**, 95% CI [{r['ci95_pp'][0]:+.1f}, {r['ci95_pp'][1]:+.1f}], p={r['p_value']}")


def render(r: dict) -> str:
    p = r["pilot"]
    lines = [
        "# Impact report",
        "",
        "Population: every viewer with at least one trigger on the replay day, analysed by **randomized arm** "
        f"(intention to treat): {r['population']['agent']} agent-arm viewers, {r['population']['holdout']} holdout "
        "viewers. Holdout viewers received no action; the agent still committed a *shadow goal* for them with the "
        "same deterministic policy, so goal success can be compared like for like.",
        "",
        "## Primary metric (declared before looking at results)",
        "",
        "Share of viewers whose first committed goal met its success rule inside its window, agent vs holdout:",
        "",
        f"- {_fmt(r['primary_goal_success_itt'])}",
        f"- Goal assignment rate: agent {r['goal_assignment_rate']['agent']:.1%}, holdout "
        f"{r['goal_assignment_rate']['holdout']:.1%}. Goals never depend on the arm; the gap comes from viewers whose "
        f"only triggers were buffered offline (agent {r['stale_only_viewers']['agent']}, holdout "
        f"{r['stale_only_viewers']['holdout']}), a property of the trigger stream fixed before the agent acts. No goal "
        "is committed for them in either arm.",
        "",
        "Per goal (first goal per viewer, rule met):",
        "",
        "| Goal | Agent | Holdout | Diff (pp) | 95% CI | p |",
        "|---|---|---|---|---|---|",
    ]
    for gid, x in r["per_goal"].items():
        lines.append(f"| {gid} | {100 * x['rate_a']:.1f}% ({x['a']}) | {100 * x['rate_b']:.1f}% ({x['b']}) | "
                     f"{x['diff_pp']:+.1f} | [{x['ci95_pp'][0]:+.1f}, {x['ci95_pp'][1]:+.1f}] | {x['p_value']} |")
    lines += ["", "## Secondary metrics (all triggered viewers, goal-agnostic)", "",
              "| Metric | Result |", "|---|---|"]
    for k, x in r["secondary_itt"].items():
        lines.append(f"| {k} | {_fmt(x)} |")
    lines += [
        "", "## Randomization check (pre-period)", "",
        f"- agent: {r['balance']['agent']}", f"- holdout: {r['balance']['holdout']}", "",
        "## Comparisons we do not trust, and why", "",
        f"1. **Served vs everyone else** (the pilot report's method), play within 3 days: "
        f"{_fmt(r['naive_served_vs_everyone_else_play3d'])}. 'Served' here means the viewer actually saw a personalised "
        "page, which requires them to have opened the app in the page's dialect while it was live. Those viewers are "
        "already the most active, so this compares engaged viewers with everyone else and mixes the agent's effect "
        "with selection.",
        f"2. **Served vs unserved inside the agent arm**: {_fmt(r['naive_served_vs_unserved_in_agent_arm_play3d'])}. "
        "This is the same selection problem: who gets served depends on behaviour after randomization.",
        "3. **Agent-arm success rate alone** (verdict 'succeeded'): it has no counterfactual. Many viewers would reach "
        "the goal anyway, as the holdout shows.",
        "",
        "Only the arm-level comparison is causal: the arms were randomized before the replay, every viewer is counted "
        "in the arm they were assigned to, and the holdout received nothing.",
        "",
        "## Per-viewer attribution (agent arm)", "",
        f"- Verdicts: {r['verdicts_agent_arm']}",
        f"- Attribution of rule-met goals: {r['attribution_agent_arm']}",
        "- 'in_flight': a purchase within 10 minutes of the decision (the purchase flow was already open). "
        "'no_prior_exposure': the signal happened before the viewer saw anything from the agent. Neither is "
        "credited, but both still count in the ITT comparison above, because ITT does not need attribution.",
        "",
        "## Re-analysis of the earlier pilot", "",
        f"- Report headline (served vs all others): {_fmt(p['report_claim_served_vs_others'])}",
        f"- **Correct ITT (treatment arm vs holdout arm): {_fmt(p['itt_treatment_vs_holdout'])}**",
        f"- Unserved treatment viewers vs holdout: {_fmt(p['treatment_unserved_vs_holdout'])}",
        "",
        "The pilot's 2.3x uplift is a selection effect. 'Served' viewers were the ones who came back and saw a page; "
        "they were compared with a group that includes inactive viewers and the whole holdout. Compared by arm, the "
        "pilot shows no positive effect. The unserved treatment viewers doing *worse* than the holdout is the "
        "signature of selection: the served group took the engaged viewers out of the treatment arm.",
        "",
        "## Caveats", "",
        "- Outcomes come from the supplied simulator with one seed. Evaluators use another seed; the ITT design is "
        "unchanged, but the numbers will move.",
        "- One replay day gives a few hundred viewers per goal, so per-goal intervals are wide.",
        "- Goals were committed only on live (fresh) triggers. Viewers whose only triggers were buffered offline "
        "have no goal in either arm.",
    ]
    return "\n".join(lines) + "\n"
