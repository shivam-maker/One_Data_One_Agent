"""One agent per viewer.

AgentSystem holds shared, read-mostly dependencies (pack, goal registry, gateways, verifier, state store) and routes
every trigger, live event and plan step for a viewer to that viewer's ViewerAgent under a per-viewer lock. The
ViewerAgent is the only component that changes a viewer's goal, plan and state document.
"""
from __future__ import annotations

import threading
from collections import defaultdict
from datetime import datetime, timedelta

from . import content
from .config import Settings
from .events import EventStore, normalize
from .facts import Facts, build_facts
from .goals import GoalRegistry
from .llm import ModelGateway
from .pack import Pack
from .planner import choose_goal, evidence_summary
from .runtime import DecisionClock, DependencyError, DependencyFaults, StoreGateway
from .store import StateStore
from .timeutil import iso, ist_day, next_send_slot, parse
from .verifier import Verifier, live

NUDGE_GOALS = {"G01", "G03", "G07", "G12", "G04"}
TERMINAL = {"succeeded", "failed", "inconclusive", "superseded"}


class AgentSystem:
    def __init__(self, settings: Settings, pack: Pack, events: EventStore, state: StateStore, provider,
                 faults: DependencyFaults | None = None):
        self.s = settings
        self.pack = pack
        self.registry = GoalRegistry(pack.goals_doc)
        self.events = events
        self.state = state
        self.faults = faults or DependencyFaults()
        self.store_gw = StoreGateway(events, self.faults, settings.store_timeout_ms)
        self.models = ModelGateway(provider, self.faults, settings.model_timeout_ms)
        self.verifier = Verifier(pack, self.registry)
        self.history_start = parse(settings.history_start)
        self._locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
        self.timers: list[dict] = []  # scheduled plan steps {due_at, user_id, decision_id, step}
        self.injected = {t.title_id: t.injection for t in pack.injected_titles()}

    def agent(self, user_id: str) -> "ViewerAgent":
        return ViewerAgent(self, user_id)

    def lock(self, user_id: str) -> threading.Lock:
        return self._locks[user_id]

    # Routing ------------------------------------------------------------------------------------------------
    def handle_trigger(self, trigger: dict, queue_ms: int = 0) -> dict:
        with self.lock(trigger["user_id"]):
            return self.agent(trigger["user_id"]).on_trigger(trigger, queue_ms)

    def observe(self, event: dict) -> dict | None:
        n = self.events.ingest(event)
        if n is None:
            return None
        with self.lock(event["user_id"]):
            return self.agent(event["user_id"]).on_event(n)

    def run_step(self, timer: dict, now: datetime) -> dict | None:
        with self.lock(timer["user_id"]):
            return self.agent(timer["user_id"]).on_plan_step(timer, now)

    # Control events -----------------------------------------------------------------------------------------
    def on_control(self, c: dict) -> dict:
        from .runtime import FaultWindow

        at = parse(c["at"])
        kind = c["type"]
        summary: dict = {"type": kind, "at": c["at"], "affected_viewers": [], "affected_decisions": []}
        if kind in ("dependency_unavailable", "dependency_degraded"):
            self.faults.add(FaultWindow(c["dependency"], "unavailable" if kind.endswith("unavailable") else "degraded",
                                        at, parse(c["until"]), c.get("detail", "")))
            summary["note"] = f"{c['dependency']} {kind.split('_')[1]} until {c['until']}; decisions in the window " \
                              f"use fallbacks (see decisions with degraded=true)"
        elif kind == "title_withdrawn":
            self.pack.withdraw(c["title_id"], at)
            hit = []
            for a in self.state.actions():
                if a["type"] == "home_page" and a.get("status") == "published" and live([a], at) and (
                        c["title_id"] == a.get("hero_title_id") or any(
                        c["title_id"] in r["title_ids"] for r in a.get("rows", []))):
                    hit.append(a)
            for a in hit:
                summary["affected_viewers"].append(a["user_id"])
                summary["affected_decisions"].append(a["decision_id"])
                with self.lock(a["user_id"]):
                    d = self.agent(a["user_id"]).republish_without(a, c["title_id"], at)
                summary.setdefault("republished", []).append(d["decision_id"])
            summary["note"] = (f"{len(hit)} live page(s) contained {c['title_id']}; each owning agent republished "
                               "or withdrew its page. Pending nudges re-check availability when they run.")
        elif kind == "goal_rule_corrected":
            v = self.registry.correct(c["goal_id"], c["new_success"], at, c.get("note", ""))
            affected = [g for g in self.state.goals(goal_id=c["goal_id"]) if g["status"] in ("open", "achieved_provisional")]
            for g in affected:
                with self.lock(g["user_id"]):
                    self.agent(g["user_id"]).apply_rule_correction(g, v, at)
                summary["affected_viewers"].append(g["user_id"])
            summary["note"] = (f"{c['goal_id']} success rule -> {v['version']}; re-pointed {len(affected)} open goal(s) "
                               f"only (out of {len(self.state.goals())} goal instances)")
            summary["new_rule_version"] = v["version"]
        with self.state.tx():
            self.state.audit(c["at"], None, f"control:{kind}", summary)
        return summary


class ViewerAgent:
    def __init__(self, sys_: AgentSystem, user_id: str):
        self.sys = sys_
        self.s = sys_.s
        self.user = user_id
        self.doc, self.version = sys_.state.load_viewer(user_id)
        self.arm = sys_.pack.arms.get(user_id)
        self._pending_actions: list[dict] = []
        self._pending_goals: list[dict] = []

    # ======================================================================================================
    # Triggers
    # ======================================================================================================
    def on_trigger(self, trig_raw: dict, queue_ms: int) -> dict:
        st = self.sys.state
        deliver_at = parse(trig_raw.get("deliver_at") or trig_raw["received_at"])
        if not st.ledger_claim(trig_raw["event_id"], self.user, iso(deliver_at)):
            led = st.ledger_get(trig_raw["event_id"])
            with st.tx():
                st.audit(iso(deliver_at), self.user, "duplicate_trigger",
                         {"event_id": trig_raw["event_id"], "original_decision": led and led["decision_id"],
                          "deliveries": led and led["deliveries"]})
            return {"status": "DUPLICATE_IGNORED", "viewer_id": self.user, "event_id": trig_raw["event_id"],
                    "original_decision_id": led and led["decision_id"]}
        self.sys.events.ingest(trig_raw)
        trig = normalize(trig_raw)
        clock = DecisionClock(self.s.max_model_calls, self.s.max_tool_calls, queue_ms=queue_ms)
        start = deliver_at + timedelta(milliseconds=queue_ms)
        age_s = int((deliver_at - trig["_true_at"]).total_seconds())
        d = self._new_decision("trigger", start, {
            "event": trig["event"], "event_id": trig["event_id"], "session_id": trig.get("session_id"),
            "received_at": trig["received_at"], "deliver_at": iso(deliver_at), "event_age_s": age_s,
            "buffered_s": int(trig["_buffered_s"]), "device_clock_suspect": trig["_clock_suspect"],
            "app_dialect": trig.get("app_dialect"), "deeplink": (trig.get("properties") or {}).get("deeplink")})
        fs, facts_source = self._facts(trig, start, clock, d)
        d["facts_source"] = facts_source
        self._fill_dialect(d, fs)
        stale = age_s > self.s.stale_trigger_s

        if self.arm is None:
            return self._finish(d, clock, fs, no_action="viewer has no randomized arm; acting would break the "
                                                         "experiment, so the agent does nothing")
        # ---- goal: continue, revise or choose -------------------------------------------------------------
        goal = self._current_goal(start, fs, d)
        if goal is None:
            if stale:
                d["no_goal_reason"] = (f"trigger is {age_s}s old (buffered offline); a goal window must start from a "
                                       "live visit, so none is committed")
            elif facts_source != "live":
                d["no_goal_reason"] = ("viewer history unavailable (data store fault): a new goal is never committed on "
                                       f"{facts_source} facts because eligibility cannot be shown")
            else:
                goal = self._choose_new_goal(fs, start, clock, d, trig)
        if goal:
            d["goal"] = self._goal_view(goal)

        # ---- action ------------------------------------------------------------------------------------------
        if self.arm != "agent":
            return self._finish(d, clock, fs, status="HOLDOUT_NO_ACTION",
                                no_action="viewer is in the randomized holdout: no agent action of any kind "
                                          "(the goal is recorded as a shadow goal for measurement)")
        if stale:
            return self._finish(d, clock, fs, no_action=(
                f"trigger event is {age_s}s old (phone buffered it offline); the viewer is no longer on the home "
                "page, so a page or message now would be noise"))
        sid = trig.get("session_id")
        prev = self.doc["sessions_acted"].get(sid) if sid else None
        own_actions = self.sys.state.actions(self.user)
        live_pages = live(own_actions, start)
        if prev and any(p["decision_id"] == prev for p in live_pages):
            return self._finish(d, clock, fs, no_action=f"already acted for this session in {prev}; page still live")
        dialect = fs.get("dialect_home")
        if not dialect:
            return self._finish(d, clock, fs, no_action=fs.fact("dialect_home").resolution or "dialect unknown")
        same = [p for p in live_pages if p["dialect"] == dialect and
                (goal is None or p.get("goal_instance_id") == goal["goal_instance_id"])]
        if same:
            left = int((parse(same[-1]["published_at"]) + timedelta(minutes=120) - start).total_seconds() / 60)
            return self._finish(d, clock, fs, no_action=(f"page {same[-1]['action_id']} for this goal and dialect is "
                                                         f"still live for {left} more minutes"))
        page = content.build_home_page(self.sys.pack, fs, goal["goal_id"] if goal else None, dialect, start)
        d["notes"] += page.pop("notes")
        pub_at = start + timedelta(milliseconds=clock.elapsed_ms())
        chk = self.sys.verifier.verify_home_page(self.user, page, fs, pub_at, live_pages)
        if goal:
            self.sys.verifier.check_goal(chk, goal["goal_id"], fs, start)
        if [f["check"] for f in chk.failed] == ["duplicate_action"]:
            d["checks"] = chk.to_dict()
            return self._finish(d, clock, fs, no_action=f"identical page already live ({chk.failed[0]['detail']}); "
                                                         "republishing would be a duplicate action")
        if not chk.ok:
            d["checks_first_attempt"] = chk.to_dict()
            fallback = content.build_home_page(self.sys.pack, fs, None, dialect, start)
            fallback.pop("notes")
            chk2 = self.sys.verifier.verify_home_page(self.user, fallback, fs, pub_at, live_pages)
            if not chk2.ok:
                d["checks"] = chk2.to_dict()
                return self._finish(d, clock, fs, status="BLOCKED_BY_CHECKS",
                                    no_action="independent checks failed for the page and its fallback")
            page, chk = fallback, chk2
            d["fallback"] = "popular_in_dialect page (goal-specific page failed checks)"
        d["checks"] = chk.to_dict()
        if goal is None:
            d["goal_reason"] = d.get("goal_reason") or "no goal committed; publishing a generic in-dialect page only"
        action = self._publish_home_page(d, page, goal, start, clock)
        if sid:
            self.doc["sessions_acted"][sid] = d["decision_id"]
        if goal and goal["goal_id"] in NUDGE_GOALS:
            self._plan_nudge(d, goal, fs, start)
        return self._finish(d, clock, fs, status="PUBLISHED", action="HOME_PAGE", actions=[action])

    # ---- facts ---------------------------------------------------------------------------------------------
    def _facts(self, trig: dict | None, now: datetime, clock: DecisionClock, d: dict) -> tuple[Facts, str]:
        key = f"{self.user}:{trig['event_id'] if trig else d['decision_id']}"
        try:
            history = self.sys.store_gw.history(self.user, now, clock, key)
            fs = build_facts(self.sys.pack, self.user, trig, history, now, self.sys.history_start,
                             self.s.recent_lapse_days, self.doc["messages"])
            self.doc["facts_cache"], self.doc["facts_cache_at"] = fs.to_dict(), iso(now)
            return fs, "live"
        except DependencyError as e:
            d["degraded"] = True
            d["notes"].append(f"user data store {e.kind}: {e.detail}")
            if self.doc.get("facts_cache"):
                fs = Facts.from_dict(now, self.doc["facts_cache"])
                age = int((now - parse(self.doc["facts_cache_at"])).total_seconds())
                d["notes"].append(f"using this agent's cached facts from {self.doc['facts_cache_at']} ({age}s old)")
                # The trigger itself is fresh, first-hand evidence about the app setting.
                if trig is not None:
                    tfs = build_facts(self.sys.pack, self.user, trig, [], now, self.sys.history_start,
                                      self.s.recent_lapse_days, self.doc["messages"])
                    fs._f["dialect_home"] = tfs.fact("dialect_home")
                return fs, "agent_memory_cache"
            fs = build_facts(self.sys.pack, self.user, trig, [normalize(trig)] if trig else [], now,
                             self.sys.history_start, self.s.recent_lapse_days, self.doc["messages"])
            return fs, "trigger_and_snapshot_only"

    # ---- goals ---------------------------------------------------------------------------------------------
    def _current_goal(self, now: datetime, fs: Facts, d: dict) -> dict | None:
        g = self.doc.get("active_goal")
        if not g:
            return None
        if g["status"] == "achieved_provisional":
            self._close_goal(g, "achieved_provisional", now, "success signal observed; awaiting final judgment")
            d["plan_revision"] = f"previous goal {g['goal_id']} reached its success signal; choosing a new goal"
            return None
        if now > parse(g["deadline"]):
            self._close_goal(g, "expired", now, "deadline passed; final verdict at judgment")
            d["plan_revision"] = f"previous goal {g['goal_id']} expired; choosing a new goal"
            return None
        # Eligibility is re-checked against today's facts for goals whose premise can be invalidated.
        if g["goal_id"] in ("G04", "G05", "G06", "G08", "G09"):
            er_now = self.sys.registry.eligibility(g["goal_id"], fs, now)
            if not er_now.eligible:
                self._close_goal(g, "superseded", now, "; ".join(er_now.reasons))
                d["plan_revision"] = (f"goal {g['goal_id']} no longer fits the viewer ({'; '.join(er_now.reasons)}); "
                                      "replanning")
                return None
        d["goal_reason"] = f"continuing active goal {g['goal_instance_id']} (single active plan)"
        d["goal_status"] = "continued"
        d["evidence"] += g.get("eligibility_evidence", [])
        d["assumptions"] += g.get("assumptions", [])
        return g

    def _choose_new_goal(self, fs: Facts, now: datetime, clock: DecisionClock, d: dict, trig: dict) -> dict | None:
        avoid = {x["goal_id"] for x in self.doc["learned"] if x.get("verdict") == "failed"
                 and now - parse(x["judged_at"]) < timedelta(days=7)}
        ch = choose_goal(self.sys.registry, fs, now, avoid)
        d["goals_considered"] = ch.considered
        if ch.goal_id is None:
            d["no_goal_reason"] = ch.why
            return None
        d["goal_reason"] = ch.why
        d["assumptions"] += ch.result.assumptions
        d["evidence"] += [e for e in ch.result.evidence if e]
        conf, why = self._base_confidence(ch.result.certainty, fs, d)
        # Model review only for agent-arm viewers, only when it can reduce real uncertainty, only within budget.
        need_review = self.arm == "agent" and (len(ch.candidates) > 1 or ch.result.certainty < 1.0)
        if need_review and clock.queue_ms > self.s.shed_model_calls_queue_ms:
            d["notes"].append(f"queue delay {clock.queue_ms} ms: skipped optional model review (load shedding)")
            need_review = False
        if need_review:
            conf, why = self._model_review(ch, fs, now, clock, d, trig, conf, why)
        d["confidence"], d["confidence_reason"] = round(conf, 2), why
        g = {
            "goal_instance_id": self.sys.state.next_id("G"), "user_id": self.user, "goal_id": ch.goal_id,
            "name": self.sys.registry.goals[ch.goal_id]["name"], "arm": self.arm, "status": "open",
            "started_at": iso(now), "deadline": iso(now + self.sys.registry.window(ch.goal_id)),
            "rule_version": self.sys.registry.current_rule(ch.goal_id)["version"],
            "success_signal": self.sys.registry.current_rule(ch.goal_id)["success"],
            "decision_id": d["decision_id"], "eligibility_evidence": ch.result.evidence,
            "assumptions": ch.result.assumptions,
        }
        if ch.goal_id == "G07":
            u = [x for x in fs.get("unfinished_series") if x["days_since"] >= 3][0]
            g["target_slug"] = u["slug"]
            g["target_title_id"] = u["title_id"]
        self.doc["active_goal"] = g
        d["goal_status"] = "new"
        self._pending_goals.append(g)
        return g

    def _base_confidence(self, certainty: float, fs: Facts, d: dict) -> tuple[float, str]:
        parts = [f"eligibility certainty {certainty:.2f}"]
        c = 0.8 * certainty
        if d.get("facts_source") != "live":
            c -= 0.2
            parts.append("facts from cache (-0.20)")
        n_conf = sum(1 for f in ("dialect_home", "subscription_status", "profile_row")
                     if fs.fact(f) and fs.fact(f).conflicts)
        if n_conf:
            c -= 0.05 * n_conf
            parts.append(f"{n_conf} resolved source conflict(s) (-{0.05 * n_conf:.2f})")
        return max(0.05, min(0.95, c)), "; ".join(parts)

    def _model_review(self, ch, fs, now, clock, d, trig, conf, why):
        payload = {"proposed_goal": ch.goal_id, "candidates": ch.candidates, "facts": evidence_summary(fs)}
        r = self.sys.models.call("goal_review", payload, clock, now, trig["event_id"] if trig else None)
        rec = {"task": "goal_review", "ok": r.ok, "error": r.error, "output": r.output if r.ok else r.raw,
               "latency_ms": r.latency_ms}
        d["model"].append(rec)
        if not r.ok:
            rec["used_as"] = "ignored (failure); deterministic decision stands"
            return conf - 0.05, why + f"; model review failed ({r.error}) (-0.05)"
        out = r.output
        eligible_set = set(ch.candidates)
        if out["goal_id"] == ch.goal_id and out["eligible"]:
            rec["used_as"] = "agreement (confidence only)"
            return min(0.95, conf + 0.05), why + "; model review agrees (+0.05)"
        if out["eligible"] and out["goal_id"] not in eligible_set:
            rec["used_as"] = "REJECTED: unsupported eligibility claim"
            d["checks_extra"].append({"check": "model_claim", "result": "rejected",
                                      "detail": f"model asserted eligibility for {out['goal_id']} "
                                                f"(conf {out['confidence']}) but no event or profile supports it: "
                                                f"{self.sys.registry.eligibility(out['goal_id'], fs, now).reasons}"})
            self._audit(iso(now), "model_claim_rejected", {"decision_id": d["decision_id"], "claim": out})
            return conf, why + "; model made an unsupported claim, which was rejected (no effect on the decision)"
        rec["used_as"] = "disagreement (confidence only)"
        return conf - 0.1, why + f"; model preferred {out['goal_id']} (eligible={out['eligible']}) (-0.10)"

    def _goal_view(self, g: dict) -> dict:
        return {"id": g["goal_id"], "name": g["name"], "instance_id": g["goal_instance_id"],
                "started_at": g["started_at"], "deadline": g["deadline"], "rule_version": g["rule_version"],
                "success_signal": g["success_signal"], "target_slug": g.get("target_slug")}

    def _close_goal(self, g: dict, status: str, now: datetime, note: str) -> None:
        g = dict(g)
        if status in ("expired", "superseded"):
            g["status"] = "superseded" if status == "superseded" else "open"
        else:
            g["status"] = status
        g.setdefault("history", []).append({"at": iso(now), "event": status, "note": note})
        self.doc["goal_history"].append({"goal_instance_id": g["goal_instance_id"], "goal_id": g["goal_id"],
                                         "closed_at": iso(now), "status": status, "note": note})
        self.doc["active_goal"] = None
        self._cancel_pending(now, f"goal {g['goal_instance_id']} {status}")
        self._pending_goals.append(g)

    # ---- actions -------------------------------------------------------------------------------------------
    def _publish_home_page(self, d, page, goal, start, clock) -> dict:
        a = {"action_id": self.sys.state.next_id("A"), "decision_id": d["decision_id"], "user_id": self.user,
             "trigger_event_id": (d.get("trigger") or {}).get("event_id"), "type": "home_page",
             "published_at": None, "dialect": page["dialect"], "hero_title_id": page["hero_title_id"],
             "rows": page["rows"], "goal_instance_id": goal["goal_instance_id"] if goal else None,
             "status": "published"}
        d["content"] = ([f"title:{page['hero_title_id']}"] if page["hero_title_id"] else []) + \
                       [f"title:{t}" for r in page["rows"] for t in r["title_ids"]]
        d["evidence"] += [f"catalogue:{t.split(':')[1]}" for t in d["content"][:3]]
        d["page"] = {"hero_title_id": page["hero_title_id"], "rows": page["rows"]}
        self._pending_actions.append(a)
        return a

    def _plan_nudge(self, d: dict, goal: dict, fs: Facts, now: datetime) -> None:
        if (d.get("confidence") or 0) < self.s.min_confidence_for_message:
            d["plan"].append({"step": "conditional_nudge", "status": "not_planned",
                              "reason": f"confidence {d.get('confidence')} below {self.s.min_confidence_for_message}"})
            return
        channel = None
        if fs.get("consent_push") and not fs.get("push_token_invalid"):
            channel = "push"
        elif fs.get("consent_whatsapp"):
            channel = "whatsapp"
        if channel is None:
            d["plan"].append({"step": "conditional_nudge", "status": "not_planned",
                              "reason": "no consented, working message channel"})
            return
        due = next_send_slot(now)
        if due > parse(goal["deadline"]):
            d["plan"].append({"step": "conditional_nudge", "status": "not_planned",
                              "reason": "next allowed send slot is after the goal deadline"})
            return
        step = {"step": "conditional_nudge", "status": "scheduled", "channel": channel, "due_at": iso(due),
                "condition": "send only if the goal's success signal has not been observed by due_at and all "
                             "checks pass at send time", "goal_instance_id": goal["goal_instance_id"]}
        d["plan"].append(step)
        self.doc["plan"] = {"goal_instance_id": goal["goal_instance_id"], "decision_id": d["decision_id"],
                            "steps": d["plan"]}
        self.sys.timers.append({"due_at": iso(due), "user_id": self.user, "decision_id": d["decision_id"],
                                "goal_instance_id": goal["goal_instance_id"], "channel": channel})

    def _cancel_pending(self, now: datetime, reason: str) -> None:
        plan = self.doc.get("plan")
        if not plan:
            return
        for s in plan["steps"]:
            if s.get("status") == "scheduled":
                s["status"] = "cancelled"
                s["cancel_reason"] = reason
                s["cancelled_at"] = iso(now)
        self.doc["plan"] = None

    # ======================================================================================================
    # Plan steps (conditional nudges)
    # ======================================================================================================
    def on_plan_step(self, timer: dict, now: datetime) -> dict | None:
        g = self.doc.get("active_goal")
        due = parse(timer["due_at"])
        plan = self.doc.get("plan")
        if not plan or plan.get("decision_id") != timer["decision_id"] or not g or \
                g["goal_instance_id"] != timer["goal_instance_id"]:
            return None  # cancelled earlier; the cancellation is already recorded in the plan/audit
        clock = DecisionClock(self.s.max_model_calls, self.s.max_tool_calls)
        d = self._new_decision("plan_step", now, {"event": "plan_step_due", "due_at": timer["due_at"],
                                                  "parent_decision_id": timer["decision_id"]})
        d["goal"] = self._goal_view(g)
        d["goal_status"] = "continued"
        fs, src = self._facts(None, now, clock, d)
        d["facts_source"] = src
        step = next(s for s in plan["steps"] if s.get("step") == "conditional_nudge" and s["status"] == "scheduled")
        if src != "live":
            step["status"] = "cancelled"
            step["cancel_reason"] = "data store unavailable at send time; a message on stale facts is not sent"
            return self._finish(d, clock, fs, no_action=step["cancel_reason"])
        try:
            hist = self.sys.store_gw.history(self.user, now, clock, f"{self.user}:{d['decision_id']}:success")
        except DependencyError as e:
            step["status"] = "cancelled"
            step["cancel_reason"] = f"data store {e.kind} when checking the success signal; not sending blind"
            return self._finish(d, clock, fs, no_action=step["cancel_reason"])
        ev, _ = self.sys.registry.success_event(g, sorted(hist, key=lambda e: e["_received"]), self.sys.pack)
        if ev is not None:
            step["status"] = "cancelled"
            step["cancel_reason"] = f"success signal already observed (event:{ev['event_id']})"
            return self._finish(d, clock, fs, no_action=step["cancel_reason"])
        title = content.pick_message_title(self.sys.pack, fs, g["goal_id"], None, due)
        if title is None:
            step["status"] = "cancelled"
            step["cancel_reason"] = "no valid title in the viewer's app dialect"
            return self._finish(d, clock, fs, no_action=step["cancel_reason"])
        ch = timer["channel"]
        msg = {"action_id": None, "decision_id": d["decision_id"], "user_id": self.user, "trigger_event_id": None,
               "type": ch, "published_at": None, "_send_at": due, "send_at": iso(due), "title_id": title.title_id,
               "goal_instance_id": g["goal_instance_id"], "status": "published"}
        if ch == "push":
            r = self.sys.models.call("push_copy", {"title_name": title.name, "goal": g["name"],
                                                   "dialect": title.dialect}, clock, now, d["decision_id"])
            text = r.output if r.ok else None
            problems = self.sys.verifier.copy_problems(text["title"], text["body"], title.title_id) if text else ["no copy"]
            d["model"].append({"task": "push_copy", "ok": r.ok, "error": r.error, "output": r.output,
                               "used_as": "copy draft" if not problems else f"rejected: {problems}"})
            if problems:
                text = {"title": f"{title.name} ab dekhiye"[:40], "body": f"{title.name} ab app par dekhiye."}
                d["notes"].append("model copy rejected or unavailable; using the fixed safe template")
            msg["text"] = text
            msg["deeplink"] = f"ott://title/{title.title_id}"
        else:
            msg["template_id"] = "wa_recommend_title_v1"
            msg["variables"] = {"title_name": title.name, "dialect_name": title.dialect}
        chk = self.sys.verifier.verify_message(self.user, msg, fs, self.doc["messages"])
        d["checks"] = chk.to_dict()
        if not chk.ok:
            step["status"] = "cancelled"
            step["cancel_reason"] = f"checks failed at send time: {[f['check'] for f in chk.failed]}"
            return self._finish(d, clock, fs, status="BLOCKED_BY_CHECKS", no_action=step["cancel_reason"])
        msg["action_id"] = self.sys.state.next_id("A")
        msg.pop("_send_at")
        d["content"] = [f"title:{title.title_id}"]
        d["evidence"] += [f"catalogue:{title.title_id}", f"goal_rule:{g['goal_id']}:success",
                          f"event_history:no_success_signal_before:{iso(now)}"]
        d["confidence"] = 0.5
        d["confidence_reason"] = "follow-up nudge: goal still open at due time; message click rates are low (~1-3%)"
        self.doc["messages"].append({"action_id": msg["action_id"], "ist_day": str(ist_day(due)), "channel": ch})
        step["status"] = "done"
        step["action_id"] = msg["action_id"]
        self._pending_actions.append(msg)
        return self._finish(d, clock, fs, status="PUBLISHED", action=ch.upper(), actions=[msg])

    # ======================================================================================================
    # Live events: update memory, revise the plan when the situation changes
    # ======================================================================================================
    def on_event(self, n: dict) -> dict | None:
        now = n["_received"]
        self.doc["observed"].append({"event_id": n["event_id"], "event": n["event"], "received_at": n["received_at"],
                                     "late_s": int(n["_buffered_s"])})
        self.doc["observed"] = self.doc["observed"][-200:]
        note = None
        g = self.doc.get("active_goal")
        if n["event"] == "notification_opt_out":
            ch = (n.get("properties") or {}).get("channel")
            self.doc["learned"].append({"at": iso(now), "lesson": f"viewer opted out of {ch}", "source": n["event_id"]})
            self._cancel_pending(now, f"viewer opted out of {ch}")
            note = "opt-out: pending nudges cancelled"
        if g and g["status"] == "open":
            ev, ver = self.sys.registry.success_event(g, [n], self.sys.pack)
            if ev is not None:
                g["status"] = "achieved_provisional"
                g["provisional_event"] = n["event_id"]
                g["provisional_rule_version"] = ver
                self._cancel_pending(now, f"success signal observed: {n['event']}")
                note = f"goal {g['goal_id']} success signal observed ({n['event']}); plan closed, replan at next visit"
                self._pending_goals.append(g)
            elif n["event"] in ("purchase_completed", "subscription_renewed", "subscription_lapsed") and \
                    g["goal_id"] in ("G04", "G05", "G06", "G08", "G09", "G11"):
                self._cancel_pending(now, f"subscription changed ({n['event']})")
                self._close_goal(g, "superseded", now, f"subscription changed ({n['event']}); goal premise gone")
                note = f"{n['event']} changed the viewer's lifecycle stage; goal superseded"
        if note:
            self._audit(n["received_at"], "plan_revised", {"event_id": n["event_id"], "event": n["event"], "note": note})
        if note or self.version:
            self._save()
        return {"viewer_id": self.user, "event": n["event"], "note": note} if note else None

    # ---- control-event helpers -----------------------------------------------------------------------------
    def apply_rule_correction(self, g: dict, version: dict, at: datetime) -> None:
        g = dict(g)
        old = g["rule_version"]
        g["rule_version"] = version["version"]
        g["success_signal"] = version["success"]
        g.setdefault("history", []).append({"at": iso(at), "event": "rule_corrected", "from": old,
                                            "to": version["version"]})
        if g["status"] == "achieved_provisional":
            hist = sorted(self.sys.events.history(self.user, at), key=lambda e: e["_received"])
            ev, _ = self.sys.registry.success_event(g, hist, self.sys.pack)
            if ev is None:
                g["status"] = "open"
                g["history"].append({"at": iso(at), "event": "reopened",
                                     "note": "provisional success no longer meets the corrected rule"})
        if self.doc.get("active_goal") and self.doc["active_goal"]["goal_instance_id"] == g["goal_instance_id"]:
            self.doc["active_goal"] = g
        self._pending_goals.append(g)
        self._audit(iso(at), "goal_rule_corrected", {"goal_instance_id": g["goal_instance_id"], "from": old,
                                                    "to": version["version"], "status": g["status"]})
        self._save()

    def republish_without(self, old: dict, title_id: str, at: datetime) -> dict:
        clock = DecisionClock(self.s.max_model_calls, self.s.max_tool_calls)
        d = self._new_decision("republish", at, {"event": "control:title_withdrawn", "title_id": title_id,
                                                 "superseded_action_id": old["action_id"]})
        fs, src = self._facts(None, at, clock, d)
        d["facts_source"] = src
        self.sys.state.set_action_status(old["action_id"], "superseded")
        g = self.doc.get("active_goal")
        if g:
            d["goal"] = self._goal_view(g)
        page = content.build_home_page(self.sys.pack, fs, g["goal_id"] if g else None, old["dialect"], at)
        page.pop("notes")
        # The page being replaced was published for the dialect the app requested; keep that dialect.
        fs.add("dialect_home", old["dialect"], "published_page", parse(old["published_at"]),
               refs=[f"action:{old['action_id']}"])
        chk = self.sys.verifier.verify_home_page(self.user, page, fs, at, [])
        d["checks"] = chk.to_dict()
        d["dialect"] = {"used": old["dialect"], "source": "page being replaced"}
        if not chk.ok:
            return self._finish(d, clock, fs, no_action="withdrawn title removed; replacement page failed checks, "
                                                        "old page marked superseded")
        a = self._publish_home_page(d, page, g, at, clock)
        return self._finish(d, clock, fs, status="PUBLISHED", action="HOME_PAGE", actions=[a])

    # ======================================================================================================
    # Recording
    # ======================================================================================================
    def _new_decision(self, kind: str, at: datetime, trigger: dict) -> dict:
        self._pending_actions: list[dict] = []
        self._pending_goals: list[dict] = []
        return {"decision_id": self.sys.state.next_id("D"), "viewer_id": self.user, "arm": self.arm, "kind": kind,
                "created_at": iso(at), "trigger": trigger, "goal": None, "goal_status": None, "goal_reason": None,
                "no_goal_reason": None, "action": "NO_ACTION", "no_action_reason": None, "dialect": None,
                "content": [], "confidence": None, "confidence_reason": None, "evidence": [], "assumptions": [],
                "conflicts": [], "model": [], "checks": {"passed": [], "failed": []}, "checks_extra": [],
                "plan": [], "notes": [], "facts_source": None, "degraded": False}

    def _fill_dialect(self, d: dict, fs: Facts) -> None:
        f = fs.fact("dialect_home")
        d["dialect"] = {"used": f.value, "source": f.source, "conflicts": f.conflicts, "resolution": f.resolution,
                        "for_messages": fs.get("dialect_messages")}

    def _finish(self, d: dict, clock: DecisionClock, fs: Facts, status: str = "NO_ACTION", action: str | None = None,
                no_action: str | None = None, actions: list[dict] | None = None) -> dict:
        start = parse(d["created_at"])
        processing = clock.elapsed_ms()
        pub = start + timedelta(milliseconds=processing)
        deliver = parse((d.get("trigger") or {}).get("deliver_at")) or start
        for a in actions or []:
            a["published_at"] = iso(pub)
            if a["type"] != "home_page":
                a.setdefault("send_at", iso(pub))
        d["status"] = status
        d["action"] = action or "NO_ACTION"
        d["no_action_reason"] = no_action
        d["action_ids"] = [a["action_id"] for a in actions or []]
        d["conflicts"] = fs.conflicts()
        d["evidence"] = list(dict.fromkeys(d["evidence"]))
        d["evidence_freshness"] = self._freshness(fs)
        if d["confidence"] is None:
            d["confidence"] = 0.0 if status != "PUBLISHED" else 0.4
            d["confidence_reason"] = d["confidence_reason"] or (
                "no goal-specific decision; confidence reflects only the safety of the default action"
                if status == "PUBLISHED" else "no action taken")
        if d["goal"]:
            d["judge_by"] = {"deadline": d["goal"]["deadline"], "signal": d["goal"]["success_signal"],
                             "rule_version": d["goal"]["rule_version"]}
        d["budget"] = {"model_calls": clock.model_calls, "tool_calls": clock.tool_calls,
                       "max_model_calls": clock.max_model_calls, "max_tool_calls": clock.max_tool_calls}
        d["calls"] = clock.calls
        d["queue_ms"] = clock.queue_ms
        d["decided_in_ms"] = processing
        d["published_at"] = iso(pub)
        d["latency_ms"] = int((pub - deliver).total_seconds() * 1000)
        d["latency_budget"] = ("ok" if d["latency_ms"] <= self.s.latency_target_ms else
                               "late" if d["latency_ms"] <= self.s.latency_fail_ms else "failed")
        if any(a["type"] == "home_page" for a in actions or []):
            d["home_page_in_time"] = processing + clock.queue_ms <= self.s.home_page_soft_budget_ms
        self.doc["decision_ids"].append(d["decision_id"])
        self.doc["action_ids"] += d["action_ids"]
        self.doc["last_decision"] = {"decision_id": d["decision_id"], "at": d["created_at"], "status": status}
        st = self.sys.state
        with st.tx():
            st.put_decision(d)
            for a in actions or []:
                st.put_action(a)
            for g in self._pending_goals:
                st.put_goal(g)
            if d.get("kind") == "trigger":
                st.ledger_set_decision(d["trigger"]["event_id"], d["decision_id"])
            self.version = st.save_viewer(self.user, self.doc, self.version)
        self._pending_goals, self._pending_actions = [], []
        return d

    def _freshness(self, fs: Facts) -> dict:
        out = {}
        for name in ("dialect_home", "subscription_status", "signup_at", "consent_push", "consent_whatsapp",
                     "last_open_before", "has_played", "has_completed"):
            f = fs.fact(name)
            if f:
                out[name] = {"value": f.value, "source": f.source, "observed_at": f.observed_at, "age_s": f.age_s,
                             "status": f.status, "refs": f.refs}
        return out

    def _audit(self, at: str, kind: str, doc: dict) -> None:
        with self.sys.state.tx():
            self.sys.state.audit(at, self.user, kind, doc)

    def _save(self) -> None:
        st = self.sys.state
        with st.tx():
            for g in getattr(self, "_pending_goals", []):
                st.put_goal(g)
            self.version = st.save_viewer(self.user, self.doc, self.version)
        self._pending_goals = []
