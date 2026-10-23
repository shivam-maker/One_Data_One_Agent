"""FastAPI service: the same AgentSystem as the replay, behind HTTP.

    uvicorn oua.api:app            (or: python -m oua serve)

POST /v1/triggers          an app_open / login_success event -> the agent's decision (idempotent on event_id)
POST /v1/events            any other live event -> memory update / plan revision
POST /v1/control           control events (title_withdrawn, goal_rule_corrected, dependency_*)
POST /v1/plan-steps/run    run plan steps due at or before `now`
GET  /v1/viewers/{id}      durable state: goal, plan, history, lessons
GET  /v1/viewers/{id}/explain?at=...   what the agent knew, decided and observed up to a moment
GET  /v1/decisions/{id}    one decision record
POST /v1/replay            run the full replay into a directory (evaluation convenience)

Dashboard (GET /) and its helpers: /v1/summary, /v1/decisions, /v1/titles, /v1/demo/seed, /v1/admin/reset
"""
from __future__ import annotations

import asyncio
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse

from .config import ROOT, Settings
from .timeutil import iso, parse


def create_app(settings: Settings | None = None, db_path: Path | str | None = None, reset: bool = False) -> FastAPI:
    from .replay import build_system

    settings = settings or Settings()
    db_path = db_path or os.environ.get("OUA_STATE_DB", ROOT / "api_state" / "state.db")
    app = FastAPI(title="One User, One Agent", version="1.0")
    app.state.system = None

    def system():
        if app.state.system is None:
            app.state.system = build_system(settings, db_path, reset=reset)
        return app.state.system

    @app.get("/health")
    def health():
        s = system()
        return {"ok": True, "provider": settings.provider, "viewers_with_state": len(s.state.viewers()),
                "fault_windows": [w.__dict__ | {"start": iso(w.start), "until": iso(w.until)} for w in s.faults.windows]}

    @app.post("/v1/triggers")
    async def trigger(event: dict[str, Any] = Body(...)):
        for k in ("event_id", "user_id", "event", "received_at"):
            if k not in event:
                raise HTTPException(422, f"missing {k}")
        if event["event"] not in ("app_open", "login_success"):
            raise HTTPException(422, "triggers are app_open or login_success; send other events to /v1/events")
        event.setdefault("deliver_at", iso(datetime.now(timezone.utc)))
        try:
            return await asyncio.wait_for(asyncio.to_thread(system().handle_trigger, event),
                                          timeout=settings.latency_target_ms / 1000)
        except asyncio.TimeoutError:
            # The decision may still finish and be recorded; the caller gets a safe "no action" answer in budget.
            return {"status": "NO_ACTION", "viewer_id": event["user_id"],
                    "no_action_reason": "decision exceeded the latency budget; no action"}

    @app.post("/v1/events")
    def live_event(event: dict[str, Any] = Body(...)):
        r = system().observe(event)
        return r or {"ok": True}

    @app.post("/v1/control")
    def control(c: dict[str, Any] = Body(...)):
        return system().on_control(c)

    @app.post("/v1/plan-steps/run")
    def run_steps(now: str | None = None):
        s = system()
        at = parse(now) if now else datetime.now(timezone.utc)
        due = [t for t in s.timers if parse(t["due_at"]) <= at]
        s.timers = [t for t in s.timers if parse(t["due_at"]) > at]
        return [s.run_step(t, parse(t["due_at"])) for t in due]

    @app.get("/v1/viewers/{viewer_id}")
    def viewer(viewer_id: str):
        s = system()
        doc, ver = s.state.load_viewer(viewer_id)
        if not ver:
            raise HTTPException(404, "no state for this viewer")
        doc = dict(doc)
        doc.pop("facts_cache", None)
        return {"state_version": ver, "arm": s.pack.arms.get(viewer_id), **doc,
                "goals": s.state.goals(user_id=viewer_id)}

    @app.get("/v1/viewers/{viewer_id}/explain")
    def explain(viewer_id: str, at: str | None = None):
        s = system()
        cut = at or "9999"
        ds = [d for d in s.state.decisions(viewer_id) if d["created_at"] <= cut]
        last = ds[-1] if ds else None
        return {
            "viewer_id": viewer_id, "as_of": at,
            "knew": last and {"facts_source": last["facts_source"], "evidence_freshness": last["evidence_freshness"],
                              "conflicts": last["conflicts"], "assumptions": last["assumptions"]},
            "decided": [{k: d.get(k) for k in ("decision_id", "created_at", "status", "action", "goal", "goal_reason",
                                               "no_action_reason", "confidence", "confidence_reason", "checks",
                                               "plan")} for d in ds],
            "observed": [x for x in s.state.audit_log(viewer_id) if x["at"] <= cut],
            "actions": [a for a in s.state.actions(viewer_id) if (a["published_at"] or "") <= cut],
        }

    @app.get("/v1/decisions/{decision_id}")
    def decision(decision_id: str):
        d = system().state.get_decision(decision_id)
        if not d:
            raise HTTPException(404, "unknown decision")
        return d

    @app.post("/v1/replay")
    def replay(out: str = "api_replay"):
        from .replay import run_replay

        r = run_replay(settings, ROOT / out, log=lambda *a: None)
        return {"out": str(ROOT / out), "counts": r["counts"]}

    # ---- dashboard support ---------------------------------------------------------------------------------
    @app.get("/", include_in_schema=False)
    def dashboard():
        return FileResponse(STATIC / "index.html")

    @app.get("/v1/summary")
    def summary():
        s = system()
        ds = s.state.decisions()
        trig = [d for d in ds if d["kind"] == "trigger"]
        acts = [a for a in s.state.actions() if a["status"] in ("published", "superseded")]
        lat = sorted(d["latency_ms"] for d in ds)
        goals = Counter(g["goal_id"] for g in s.state.goals() if g["arm"] == "agent")
        return {
            "viewers": len(s.state.viewers()), "decisions": len(ds), "trigger_decisions": len(trig),
            "status": dict(Counter(d["status"] for d in ds)),
            "actions": dict(Counter(a["type"] for a in acts)),
            "duplicates_ignored": len(s.state.audit_log(kind="duplicate_trigger")),
            "model_claims_rejected": len(s.state.audit_log(kind="model_claim_rejected")),
            "model_failures": sum(1 for d in ds for m in d["model"] if not m["ok"]),
            "plan_revisions": len(s.state.audit_log(kind="plan_revised")),
            "goals_committed": dict(sorted(goals.items())),
            "latency_ms": {"p50": lat[len(lat) // 2] if lat else None,
                           "p95": lat[min(len(lat) - 1, int(len(lat) * 0.95))] if lat else None,
                           "max": lat[-1] if lat else None},
            "provider": settings.provider,
        }

    @app.get("/v1/decisions")
    def list_decisions(limit: int = 200, viewer: str | None = None, status: str | None = None):
        ds = system().state.decisions(viewer)
        if status:
            ds = [d for d in ds if d["status"] == status]
        out = []
        for d in reversed(ds[-limit:] if not status else ds[-limit:]):
            out.append({"decision_id": d["decision_id"], "created_at": d["created_at"], "viewer_id": d["viewer_id"],
                        "arm": d["arm"], "kind": d["kind"], "status": d["status"], "action": d["action"],
                        "goal": (d.get("goal") or {}).get("id"), "confidence": d.get("confidence"),
                        "latency_ms": d.get("latency_ms"),
                        "reason": d.get("no_action_reason") or (
                            f"follow-up message: goal still open at {d['trigger'].get('due_at', '')[11:16]} UTC"
                            if d["kind"] == "plan_step" else None) or d.get("goal_reason") or d.get("no_goal_reason")})
        return out

    @app.get("/v1/titles")
    def titles(ids: str):
        pack = system().pack
        out = {}
        for tid in ids.split(",")[:200]:
            t = pack.titles.get(tid.strip())
            if t:
                out[t.title_id] = {"name": t.name, "dialect": t.dialect, "language": t.language,
                                   "type": t.content_type, "genres": t.genres, "premium": t.is_premium}
        return out

    @app.post("/v1/demo/seed")
    def demo_seed(replay_triggers: int = 60):
        from .demo import run_demo

        def send(method, path, body):
            s = system()
            if path == "/v1/triggers":
                return s.handle_trigger(dict(body))
            if path == "/v1/events":
                return s.observe(body) or {"ok": True}
            if path == "/v1/control":
                return s.on_control(body)
            if path.startswith("/v1/plan-steps/run"):
                return run_steps(path.split("now=", 1)[1])
            raise ValueError(path)

        return run_demo(send, settings.pack_dir, replay_triggers)

    @app.post("/v1/admin/reset")
    def admin_reset():
        if app.state.system is not None:
            app.state.system.state.db.close()
        app.state.system = build_system(settings, db_path, reset=True)
        return {"ok": True, "viewers_with_state": 0}

    return app


STATIC = Path(__file__).resolve().parent / "static"


app = create_app(reset=os.environ.get("OUA_RESET") == "1")
