"""Replay harness: feeds the replay day to the agent system in delivery order and writes sample_run outputs.

Ordering: triggers by deliver_at, live events by deliver_at, control events by at, and plan-step timers by due_at,
merged into one timeline. At equal times control events go first, then live events, then timers, then triggers.

Concurrency: decisions run on a pool of simulated workers. A trigger starts when it is delivered and a worker is free;
the wait is recorded as queue_ms and counts toward published_at (deliver_at + queue + processing).
"""
from __future__ import annotations

import heapq
import json
import shutil
import statistics
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from .agent import AgentSystem
from .config import Settings
from .events import EventStore, read_jsonl
from .llm import make_provider
from .pack import Pack
from .runtime import DependencyFaults
from .store import StateStore
from .timeutil import iso, parse

PRIO = {"control": 0, "live": 1, "timer": 2, "trigger": 3}
INTERNAL_ACTION_KEYS = {"status", "goal_instance_id"}


def build_system(settings: Settings, db_path: Path | str, reset: bool = True) -> AgentSystem:
    if reset and str(db_path) != ":memory:":
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(db_path) + suffix)
            if p.exists():
                p.unlink()
    pack = Pack.load(settings.pack_dir)
    events = EventStore()
    events.ingest_many(read_jsonl(settings.pack_dir / "data/events.jsonl"))
    pack.compute_popularity(events.all_events())
    faults_by_trigger = {f["trigger_event_id"]: f for f in read_jsonl(settings.pack_dir / "replay/model_faults.jsonl")}
    provider = make_provider(settings.provider, faults_by_trigger)
    return AgentSystem(settings, pack, events, StateStore(db_path), provider, DependencyFaults())


def run_replay(settings: Settings, out_dir: Path, replay_dir: Path | None = None, log=print) -> dict:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    replay_dir = Path(replay_dir or settings.pack_dir / "replay")
    t0 = time.perf_counter()
    system = build_system(settings, out_dir / "state.db")
    log(f"loaded pack and {sum(1 for _ in system.events.all_events())} history events in "
        f"{time.perf_counter() - t0:.1f}s; injected-text titles quarantined: {sorted(system.injected)}")
    for tid, frag in system.injected.items():
        system.state.audit("2026-09-30T00:00:00.000Z", None,
                           "security:catalogue_text_quarantined", {"title_id": tid, "matched": frag,
                                                                   "handling": "title excluded from all pages and "
                                                                               "messages; text never sent to a model"})

    heap: list = []
    seq = 0

    def push(at: datetime, kind: str, item: dict):
        nonlocal seq
        seq += 1
        heapq.heappush(heap, (at, PRIO[kind], seq, kind, item))

    for t in read_jsonl(replay_dir / "triggers.jsonl"):
        push(parse(t["deliver_at"]), "trigger", t)
    for e in read_jsonl(replay_dir / "live_events.jsonl"):
        push(parse(e.get("deliver_at") or e["received_at"]), "live", e)
    for c in read_jsonl(replay_dir / "control_events.jsonl"):
        push(parse(c["at"]), "control", c)
    replay_end = max(x[0] for x in heap)

    workers = [parse("2000-01-01T00:00:00Z")] * settings.workers
    counts = Counter()
    controls = []
    while heap:
        at, _, _, kind, item = heapq.heappop(heap)
        if kind == "trigger":
            i = min(range(len(workers)), key=lambda k: workers[k])
            start = max(at, workers[i])
            queue_ms = int((start - at).total_seconds() * 1000)
            d = system.handle_trigger(item, queue_ms)
            if d.get("status") == "DUPLICATE_IGNORED":
                counts["duplicate_trigger_ignored"] += 1
                continue
            workers[i] = start + timedelta(milliseconds=d["decided_in_ms"])
            counts[f"decision:{d['status']}"] += 1
        elif kind == "live":
            before = system.events.duplicates_dropped
            r = system.observe(item)
            counts["live_event_duplicate_dropped" if system.events.duplicates_dropped > before else "live_event"] += 1
            if r and r.get("note"):
                counts["plan_revised_by_live_event"] += 1
        elif kind == "control":
            s = system.on_control(item)
            controls.append(s)
            log(f"control {item['type']} at {item['at']}: {s.get('note')}")
        elif kind == "timer":
            d = system.run_step(item, at)
            counts["plan_step:" + (d["status"] if d else "already_cancelled")] += 1
        # Newly scheduled plan steps join the timeline (or run at replay end with what is known then).
        while system.timers:
            tm = system.timers.pop()
            due = parse(tm["due_at"])
            push(min(due, replay_end), "timer", tm)

    log(f"replay finished in {time.perf_counter() - t0:.1f}s: {dict(counts)}")
    export(system, out_dir, controls, counts)
    return {"counts": dict(counts), "controls": controls, "system": system}


def export(system: AgentSystem, out_dir: Path, controls: list[dict], counts: Counter) -> None:
    st = system.state
    decisions = st.decisions()
    with open(out_dir / "decisions.jsonl", "w", encoding="utf-8") as f:
        for d in decisions:
            f.write(json.dumps(d, ensure_ascii=False, default=str) + "\n")
    actions = [a for a in st.actions() if a["status"] in ("published", "superseded")]
    with open(out_dir / "actions.jsonl", "w", encoding="utf-8") as f:
        for a in actions:
            f.write(json.dumps({k: v for k, v in a.items() if k not in INTERNAL_ACTION_KEYS},
                               ensure_ascii=False) + "\n")
    with open(out_dir / "control_log.json", "w", encoding="utf-8") as f:
        json.dump(controls, f, indent=2, default=str)
    with open(out_dir / "audit.jsonl", "w", encoding="utf-8") as f:
        for r in st.audit_log():
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    export_viewer_state(system, out_dir)
    write_latency_report(decisions, out_dir / "latency_report.json", system)


def export_viewer_state(system: AgentSystem, out_dir: Path) -> None:
    vdir = out_dir / "viewer_state"
    if vdir.exists():
        shutil.rmtree(vdir)
    vdir.mkdir(parents=True)
    st = system.state
    goals_by_user: dict[str, list] = {}
    for g in st.goals():
        goals_by_user.setdefault(g["user_id"], []).append(g)
    for u in st.viewers():
        doc, ver = st.load_viewer(u)
        doc = dict(doc)
        doc["state_version"] = ver
        doc["arm"] = system.pack.arms.get(u)
        doc["goals"] = goals_by_user.get(u, [])
        doc.pop("facts_cache", None)  # facts are inside each decision; keep the per-viewer file readable
        (vdir / f"{u}.json").write_text(json.dumps(doc, indent=1, default=str), encoding="utf-8")


def _pct(xs, p):
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


def write_latency_report(decisions: list[dict], path: Path, system: AgentSystem) -> dict:
    trig = [d for d in decisions if d["kind"] == "trigger"]
    lat = [d["latency_ms"] for d in trig]
    proc = [d["decided_in_ms"] for d in trig]
    queue = [d["queue_ms"] for d in trig]
    pages = [d for d in trig if d["action"] == "HOME_PAGE"]

    def window(name, a, b):
        sel = [d for d in trig if a <= d["trigger"]["deliver_at"] < b]
        ls = [d["latency_ms"] for d in sel]
        return {"window": name, "from": a, "to": b, "decisions": len(sel), "p50_ms": _pct(ls, 50),
                "p95_ms": _pct(ls, 95), "max_ms": max(ls) if ls else None,
                "degraded_decisions": sum(1 for d in sel if d.get("degraded")),
                "model_failures": sum(1 for d in sel for m in d["model"] if not m["ok"]),
                "published": sum(1 for d in sel if d["status"] == "PUBLISHED")}

    per_min = Counter(d["trigger"]["deliver_at"][:16] for d in trig)
    windows = [window("model_provider_unavailable", "2026-10-01T10:35", "2026-10-01T10:42"),
               window("user_data_store_degraded", "2026-10-01T13:10", "2026-10-01T13:17")]
    peak_min, peak_n = per_min.most_common(1)[0]
    rep = {
        "decisions": len(trig),
        "latency_ms": {"p50": _pct(lat, 50), "p95": _pct(lat, 95), "p99": _pct(lat, 99), "max": max(lat),
                       "mean": round(statistics.mean(lat), 1)},
        "processing_ms": {"p50": _pct(proc, 50), "p95": _pct(proc, 95), "max": max(proc)},
        "queue_ms": {"p50": _pct(queue, 50), "p95": _pct(queue, 95), "max": max(queue)},
        "within_60s": sum(1 for x in lat if x <= 60_000), "over_60s": sum(1 for x in lat if x > 60_000),
        "over_5min_failures": sum(1 for x in lat if x > 300_000),
        "home_pages": len(pages),
        "home_pages_within_3s_soft_budget": sum(1 for d in pages if d.get("home_page_in_time")),
        "budget": {"max_model_calls_used": max(d["budget"]["model_calls"] for d in decisions),
                   "max_tool_calls_used": max(d["budget"]["tool_calls"] for d in decisions),
                   "limit_model_calls": system.s.max_model_calls, "limit_tool_calls": system.s.max_tool_calls},
        "fault_windows": windows,
        "busiest_minute": {"minute": peak_min, "triggers": peak_n,
                           **window("busiest_minute", peak_min, peak_min + ":60")},
        "evening_peak": window("evening_peak_15-16Z", "2026-10-01T15:00", "2026-10-01T16:00"),
        "note": "latency = published_at - deliver_at = queue wait + measured wall time of our code + simulated "
                "dependency latency (model, data store), with timeouts of "
                f"{system.s.model_timeout_ms} ms (model) and {system.s.store_timeout_ms} ms (store).",
    }
    path.write_text(json.dumps(rep, indent=2), encoding="utf-8")
    return rep
