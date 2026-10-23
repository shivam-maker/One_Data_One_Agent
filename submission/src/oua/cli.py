"""Command line entry point.

    python -m oua all       --out sample_run       replay -> simulate -> judge -> impact -> final report
    python -m oua replay    --out sample_run       replay the trigger script only
    python -m oua simulate  --out sample_run       run the pack's outcome simulator on sample_run/actions.jsonl
    python -m oua judge     --out sample_run       judge goals + impact report from an existing run
    python -m oua explain   --out sample_run --viewer U-00042 [--at 2026-10-01T12:00:00Z]
    python -m oua serve                            FastAPI service (see api.py)
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

from .config import ROOT, Settings
from .events import read_jsonl
from .timeutil import parse


def _settings(a) -> Settings:
    s = Settings()
    if a.pack:
        s.pack_dir = Path(a.pack).resolve()
    if getattr(a, "provider", None):
        s.provider = a.provider
    if getattr(a, "workers", None):
        s.workers = a.workers
    return s


def cmd_replay(a):
    from .replay import run_replay

    return run_replay(_settings(a), Path(a.out), Path(a.replay_dir) if a.replay_dir else None)


def cmd_simulate(a):
    s = _settings(a)
    out = Path(a.out).resolve()
    cmd = [sys.executable, str(s.pack_dir / "simulator/simulate_outcomes.py"), "--pack", str(s.pack_dir),
           "--actions", str(out / "actions.jsonl"), "--out", str(out / "sim"), "--seed", a.seed]
    print("running:", " ".join(cmd))
    subprocess.run(cmd, check=True)


def load_system(s: Settings, out: Path, replay_dir: Path | None = None):
    """Re-open a finished run (state.db) and re-apply the run's control events to the in-memory registry/pack."""
    from .replay import build_system

    system = build_system(s, out / "state.db", reset=False)
    for c in read_jsonl(Path(replay_dir or s.pack_dir / "replay") / "control_events.jsonl"):
        if c["type"] == "goal_rule_corrected":
            system.registry.correct(c["goal_id"], c["new_success"], parse(c["at"]), c.get("note", ""))
        elif c["type"] == "title_withdrawn":
            system.pack.withdraw(c["title_id"], parse(c["at"]))
    return system


def cmd_judge(a, system=None):
    from .impact import analyse
    from .judge import judge_all
    from .replay import export_viewer_state

    s = _settings(a)
    out = Path(a.out)
    system = system or load_system(s, out, Path(a.replay_dir) if a.replay_dir else None)
    outcomes = judge_all(system, out / "sim", out / "outcomes.jsonl")
    result = analyse(system, out / "sim", outcomes, out / "impact_report.md")
    export_viewer_state(system, out)
    write_final_report(system, out, result)
    p = result["primary_goal_success_itt"]
    print(f"judged {len(outcomes)} goals; primary ITT diff {p['diff_pp']:+.2f} pp, CI {p['ci95_pp']}, p={p['p_value']}")


def cmd_all(a):
    r = cmd_replay(a)
    cmd_simulate(a)
    cmd_judge(a, r["system"])


def cmd_explain(a):
    """What did the agent know, decide and observe for one viewer, up to a moment?"""
    s = _settings(a)
    out = Path(a.out)
    from .store import StateStore

    st = StateStore(out / "state.db")
    at = a.at or "9999"
    ds = [d for d in st.decisions(a.viewer) if d["created_at"] <= at]
    view = {
        "viewer_id": a.viewer, "as_of": a.at,
        "decisions": [{k: d.get(k) for k in ("decision_id", "kind", "created_at", "status", "action", "goal",
                                             "goal_reason", "no_goal_reason", "no_action_reason", "dialect",
                                             "content", "confidence", "confidence_reason", "evidence_freshness",
                                             "checks", "plan", "published_at", "latency_ms")} for d in ds],
        "actions": [x for x in st.actions(a.viewer) if x["published_at"] <= at],
        "audit": [x for x in st.audit_log(a.viewer) if x["at"] <= at],
        "goals": st.goals(user_id=a.viewer),
    }
    print(json.dumps(view, indent=1, default=str))


def cmd_serve(a):
    import os

    import uvicorn

    if a.reset:
        os.environ["OUA_RESET"] = "1"  # start the API with empty agent state
    uvicorn.run("oua.api:app", host=a.host, port=a.port, reload=False)


def write_final_report(system, out: Path, impact: dict) -> None:
    D = system.state.decisions()
    trig = [d for d in D if d["kind"] == "trigger"]
    lat = json.loads((out / "latency_report.json").read_text(encoding="utf-8"))
    sim = json.loads((out / "sim/simulator_report.json").read_text(encoding="utf-8")) \
        if (out / "sim/simulator_report.json").exists() else {}
    controls = json.loads((out / "control_log.json").read_text(encoding="utf-8"))
    dup = len(system.state.audit_log(kind="duplicate_trigger"))
    status = Counter(d["status"] for d in trig)
    actions = Counter(a["type"] for a in system.state.actions() if a["status"] in ("published", "superseded"))
    reasons = Counter()
    for d in trig:
        if d["arm"] == "agent" and d["action"] == "NO_ACTION":
            r = d["no_action_reason"] or ""
            key = ("stale/buffered trigger" if "buffered" in r else "unsupported app dialect" if "unsupported dialect" in r
                   else "page already live (idempotent)" if "still live" in r or "already acted" in r or "identical" in r
                   else "no arm" if "no randomized arm" in r else r[:60])
            reasons[key] += 1
    goals = Counter((d["arm"], d["goal"]["id"]) for d in trig if d["goal"] and d["goal_status"] == "new")
    rejected = [d["decision_id"] for d in D for c in d.get("checks_extra", []) if c.get("check") == "model_claim"]
    model_fail = [(d["decision_id"], m["error"]) for d in D for m in d["model"] if not m["ok"]]
    degraded = [d["decision_id"] for d in D if d.get("degraded")]
    stale = [d for d in trig if d["trigger"]["event_age_s"] > system.s.stale_trigger_s]
    three = [d for d in trig if len({c.split(":")[0] for c in (d["dialect"] or {}).get("conflicts", [])}) >= 2]
    purchase_rev = system.state.audit_log(kind="plan_revised")
    p = impact["primary_goal_success_itt"]
    pi = impact["pilot"]["itt_treatment_vs_holdout"]
    lines = [
        "# Final report: replay of 2026-10-01", "",
        "## Headline", "",
        f"- {len(trig)} trigger decisions for {len({d['viewer_id'] for d in trig})} viewers; {dup} duplicate "
        "deliveries ignored idempotently (no second decision).",
        f"- Decision status: {dict(status)}",
        f"- Published actions: {dict(actions)}",
        f"- Latency: p50 {lat['latency_ms']['p50']} ms, p95 {lat['latency_ms']['p95']} ms, max "
        f"{lat['latency_ms']['max']} ms; {lat['over_60s']} decisions over 60 s, {lat['over_5min_failures']} over 5 min.",
        f"- Policy problems reported by the simulator: **{sim.get('policy_problem_count', 'n/a')}** "
        f"(counts: {sim.get('counts', {})})",
        f"- Primary impact (ITT, first-goal success, agent vs holdout): {p['diff_pp']:+.2f} pp, 95% CI "
        f"{p['ci95_pp']}, p={p['p_value']}. See impact_report.md.",
        f"- Earlier pilot, correct ITT: {pi['diff_pp']:+.2f} pp (CI {pi['ci95_pp']}), not the +22.4 pp in its report.",
        "", "## Why the agent did nothing (agent arm)", "",
        *[f"- {k}: {v}" for k, v in reasons.most_common()],
        f"- Holdout viewers (no action by design): {status.get('HOLDOUT_NO_ACTION', 0)} decisions",
        "", "## Goals committed (new)", "",
        *[f"- {arm} {gid}: {n}" for (arm, gid), n in sorted(goals.items())],
        "- Never chosen: G10 (no success signal defined), G11 (`tv_app_open` is not in the event stream).",
        "", "## Surprise events: what was noticed and who was affected", "",
        f"- **Offline-buffered app opens**: {len(stale)} triggers older than {system.s.stale_trigger_s}s (some days "
        "old). They updated memory but committed no goal and published nothing. Age uses `sent_at - occurred_at`, "
        "which is immune to a wrong phone clock.",
        f"- **Purchase right after a decision**: {sum(1 for x in purchase_rev if 'purchase' in x.get('event', ''))} "
        "plans revised on a live purchase; pending nudges cancelled; such purchases are judged 'in_flight' "
        "(not credited) when they land within 10 minutes of the decision.",
        f"- **Three dialects (app vs profile vs campaign link)**: {len(three)} decisions; the app setting was used, "
        "the others are listed in `dialect.conflicts`. Triggers with an unsupported app dialect (None, 'NA', 'd7') "
        "got no page, because the app would never show a page in a different dialect.",
    ]
    for c in controls:
        extra = f"affected viewers: {len(c.get('affected_viewers', []))}"
        if c["type"].startswith("dependency_"):
            raw = next(x for x in read_jsonl(system.s.pack_dir / "replay/control_events.jsonl")
                       if x["at"] == c["at"] and x["type"] == c["type"])
            inwin = [d for d in D if raw["at"] <= d["created_at"] < raw["until"]]
            extra = (f"decisions in the window: {len(inwin)}, published: "
                     f"{sum(1 for d in inwin if d['status'] == 'PUBLISHED')}, model failures handled: "
                     f"{sum(1 for d in inwin for m in d['model'] if not m['ok'])}, on cached/partial facts: "
                     f"{sum(1 for d in inwin if d.get('degraded'))}, max latency "
                     f"{max((d['latency_ms'] for d in inwin), default=0)} ms")
        elif c["type"] == "goal_rule_corrected":
            gid = c.get("new_rule_version", "").split("-")[0]
            later = [g for g in system.state.goals(goal_id=gid) if g["started_at"] >= c["at"]]
            extra += (f"; {len(later)} {gid} goal(s) opened later used the corrected rule from the start. "
                      "Re-pointing open goals is covered by tests/test_agent.py::"
                      "test_goal_rule_correction_touches_only_open_goals_of_that_goal")
        lines.append(f"- **{c['type']}** at {c['at']}: {c.get('note')} {extra}")
    lines += [
        f"- **Unsupported model claims**: rejected in {rejected or 'none'}; the decision used deterministic "
        "eligibility. Fault triggers that arrived buffered offline or for holdout viewers never reached the model.",
        f"- **Model failures (timeout / malformed / 503)**: {len(model_fail)}: "
        f"{Counter(e.split(':')[0].split(' (')[0] for _, e in model_fail)}. All fell back to the deterministic path.",
        f"- **Data store degraded**: {len(degraded)} decisions used cached facts or trigger-only facts; none "
        "committed a new goal on partial facts.",
        f"- **Catalogue text with instructions**: {sorted(system.injected)} quarantined; no page or message "
        "contains them and catalogue text never reaches a model.",
        f"- **Evening peak / busiest minute**: {lat['busiest_minute']['triggers']} triggers in "
        f"{lat['busiest_minute']['minute']}, p95 latency {lat['busiest_minute']['p95_ms']} ms; evening peak p95 "
        f"{lat['evening_peak']['p95_ms']} ms.",
        "", "## Files", "",
        "- decisions.jsonl: every decision (including no-action and holdout decisions) with evidence, freshness, "
        "conflicts, checks, confidence and timing",
        "- actions.jsonl: what was published (input to the simulator)",
        "- outcomes.jsonl: goal verdicts and attribution",
        "- viewer_state/: one durable state document per viewer (goals, plan, history, lessons)",
        "- latency_report.json, impact_report.md, impact.json, audit.jsonl, control_log.json, sim/",
    ]
    (out / "final_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="oua", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("all", "replay", "simulate", "judge", "explain", "serve"):
        p = sub.add_parser(name)
        p.add_argument("--pack", default=None, help="data pack root (default: ../one_user_one_agent_pack)")
        p.add_argument("--out", default=str(ROOT / "sample_run"))
        p.add_argument("--replay-dir", default=None, help="alternative replay/ directory (unseen day)")
        p.add_argument("--provider", default=None, choices=["mock", "anthropic"])
        p.add_argument("--workers", type=int, default=None)
        p.add_argument("--seed", default="oua-practice")
        p.add_argument("--viewer")
        p.add_argument("--at")
        p.add_argument("--host", default="127.0.0.1")
        p.add_argument("--port", type=int, default=8000)
        p.add_argument("--reset", action="store_true", help="serve: start with empty agent state")
    a = ap.parse_args(argv)
    {"all": cmd_all, "replay": cmd_replay, "simulate": cmd_simulate, "judge": cmd_judge, "explain": cmd_explain,
     "serve": cmd_serve}[a.cmd](a)


if __name__ == "__main__":
    main()
