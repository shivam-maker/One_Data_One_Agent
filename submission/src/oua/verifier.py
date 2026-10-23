"""Step 5, "verify before acting": checks that are independent of the decision step.

The verifier re-derives what it needs from raw inputs (the pack, the viewer's facts, the goal registry and the
agent's own action log). It never reads the planner's reasons or any model output as evidence. A failed check
blocks the action; the agent then falls back to a smaller action or to none.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from .facts import Facts
from .goals import GoalRegistry
from .pack import VALID_DIALECTS, Pack
from .timeutil import in_quiet_hours, iso, ist_day


class CheckResult:
    def __init__(self) -> None:
        self.passed: list[str] = []
        self.failed: list[dict] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        if ok:
            if name not in self.passed:
                self.passed.append(name)
        else:
            self.failed.append({"check": name, "detail": detail})
        return ok

    @property
    def ok(self) -> bool:
        return not self.failed

    def to_dict(self) -> dict:
        return {"passed": self.passed, "failed": self.failed}


class Verifier:
    def __init__(self, pack: Pack, goals: GoalRegistry):
        self.pack, self.goals = pack, goals
        hp = pack.interventions["home_page"]["rows"]
        self.min_row, self.max_row, self.max_rows = hp["min_titles_per_row"], hp["max_titles_per_row"], hp["max_rows"]
        cp = pack.interventions["push"]["copy"]
        self.title_max, self.body_max = cp["title_max_chars"], cp["body_max_chars"]
        self.forbidden = [p.lower() for p in cp["forbidden_phrases"]]
        self.templates = {t["template_id"]: t for t in pack.interventions["whatsapp"]["approved_templates"]}

    # ---- common --------------------------------------------------------------------------------------------
    def check_arm(self, res: CheckResult, user_id: str) -> bool:
        arm = self.pack.arms.get(user_id)
        return res.check("holdout", arm == "agent", f"arm={arm}: holdout and unknown-arm viewers get no action")

    def check_goal(self, res: CheckResult, goal_id: str | None, fs: Facts, now: datetime) -> bool:
        if goal_id is None:
            return True
        er = self.goals.eligibility(goal_id, fs, now)
        return res.check("eligibility", er.eligible, "; ".join(er.reasons))

    # ---- home page -----------------------------------------------------------------------------------------
    def verify_home_page(self, user_id: str, page: dict, fs: Facts, publish_at: datetime,
                         live_pages: list[dict]) -> CheckResult:
        res = CheckResult()
        self.check_arm(res, user_id)
        d = page.get("dialect")
        res.check("dialect", d in VALID_DIALECTS and d == fs.get("dialect_home"),
                  f"page dialect {d} vs app setting {fs.get('dialect_home')}")
        titles = ([page["hero_title_id"]] if page.get("hero_title_id") else []) + \
                 [t for r in page.get("rows", []) for t in r["title_ids"]]
        bad = []
        for tid in titles:
            ok, why = self.pack.title_valid(tid, d, publish_at)
            if not ok:
                bad.append(f"{tid}:{why}")
        res.check("availability", not any("not_available" in b or "withdrawn" in b for b in bad),
                  ", ".join(b for b in bad if "not_available" in b or "withdrawn" in b))
        res.check("title_dialect", not any("wrong_dialect" in b or "unknown_title" in b for b in bad),
                  ", ".join(b for b in bad if "wrong_dialect" in b or "unknown_title" in b))
        res.check("injected_content", not any("quarantined" in b for b in bad),
                  ", ".join(b for b in bad if "quarantined" in b))
        slugs = [self.pack.titles[t].slug for t in titles if t in self.pack.titles]
        res.check("unique_content", len(slugs) == len(set(slugs)), "same content_slug appears twice")
        rows = page.get("rows", [])
        res.check("row_rules", len(rows) <= self.max_rows and all(
            self.min_row <= len(r["title_ids"]) <= self.max_row for r in rows) and (rows or page.get("hero_title_id")),
            "row count/size outside platform rules or empty page")
        same = [p for p in live_pages if p.get("dialect") == d and _page_key(p) == _page_key(page)]
        res.check("duplicate_action", not same, f"identical page already live: {[p['action_id'] for p in same]}")
        return res

    # ---- messages ------------------------------------------------------------------------------------------
    def verify_message(self, user_id: str, msg: dict, fs: Facts, published_msgs: list[dict]) -> CheckResult:
        res = CheckResult()
        self.check_arm(res, user_id)
        ch = msg["type"]
        send_at = msg["_send_at"]
        res.check("consent", bool(fs.get(f"consent_{ch}")),
                  f"consent_{ch}={fs.get(f'consent_{ch}')} via {fs.fact(f'consent_{ch}').source}")
        res.check("quiet_hours", not in_quiet_hours(send_at), f"send_at {iso(send_at)} is 22:00-08:00 IST")
        day = ist_day(send_at)
        same_day_own = [m for m in published_msgs if m.get("ist_day") == str(day)]
        delivered_today = fs.get("messages_today") if ist_day(fs.now) == day else 0
        res.check("frequency_cap", not same_day_own and delivered_today == 0,
                  f"already {len(same_day_own)} agent message(s) and {delivered_today} delivery(ies) on {day}")
        tid = msg.get("title_id")
        d = fs.get("dialect_messages")
        if tid:
            ok, why = self.pack.title_valid(tid, d, send_at)
            res.check("dialect", ok or "wrong_dialect" not in why, f"{tid}: {why}")
            res.check("availability", ok or "wrong_dialect" in why, f"{tid}: {why}")
        if ch == "push":
            res.check("push_token", not fs.get("push_token_invalid"), "push previously failed with token_invalid")
            t = msg.get("text") or {}
            problems = self.copy_problems(t.get("title", ""), t.get("body", ""), tid)
            res.check("copy_rules", not problems, "; ".join(problems))
            res.check("deeplink", msg.get("deeplink") == f"ott://title/{tid}", "deeplink must point at the title")
        if ch == "whatsapp":
            tpl = self.templates.get(msg.get("template_id"))
            res.check("template_approved", tpl is not None, f"template {msg.get('template_id')} not approved")
            if tpl:
                vars_ = msg.get("variables") or {}
                res.check("template_variables", set(vars_) == set(tpl["variables"]),
                          f"variables {sorted(vars_)} vs {tpl['variables']}")
                problems = self.template_value_problems(tpl["template_id"], vars_, tid)
                res.check("template_values", not problems, "; ".join(problems))
        return res

    def copy_problems(self, title: str, body: str, title_id: str | None) -> list[str]:
        p = []
        if not title or len(title) > self.title_max:
            p.append(f"title length {len(title)} (max {self.title_max})")
        if not body or len(body) > self.body_max:
            p.append(f"body length {len(body)} (max {self.body_max})")
        low = f"{title} {body}".lower()
        p += [f"forbidden phrase '{f}'" for f in self.forbidden if f in low]
        for num in re.findall(r"(?:₹|rs\.?\s*|inr\s*)(\d+)", low):
            if int(num) not in self.pack.valid_prices():
                p.append(f"price {num} does not exist in pricing.json")
        if re.search(r"\b(free|discount|offer|% off|cashback)\b", low):
            p.append("offer/discount claims are not allowed (none exist)")
        if title_id and title_id in self.pack.titles and self.pack.titles[title_id].name.lower() not in low:
            p.append("copy does not name the promoted title")
        return p

    def template_value_problems(self, template_id: str, vars_: dict, title_id: str | None) -> list[str]:
        p = []
        t = self.pack.titles.get(title_id) if title_id else None
        if "title_name" in vars_ and (not t or vars_["title_name"] != t.name):
            p.append("title_name must equal the catalogue name of the promoted title")
        if "dialect_name" in vars_ and (not t or vars_["dialect_name"] != t.dialect):
            p.append("dialect_name must come from the catalogue")
        if "plan_price" in vars_:
            m = re.fullmatch(r"₹(\d+)", str(vars_["plan_price"]))
            if not m or int(m.group(1)) not in self.pack.valid_prices():
                p.append("plan_price must be a price from pricing.json")
        if "episode_number" in vars_:
            p.append("new-episode template needs proof of a new episode; not supported")
        return p


def _page_key(page: dict) -> tuple:
    return (page.get("hero_title_id"), tuple(tuple(r["title_ids"]) for r in page.get("rows", [])))


def live(actions: list[dict], at: datetime, ttl_minutes: int = 120) -> list[dict]:
    from .timeutil import parse

    out = []
    for a in actions:
        if a["type"] != "home_page" or a.get("status") == "superseded":
            continue
        p = parse(a["published_at"])
        if p <= at < p + timedelta(minutes=ttl_minutes):
            out.append(a)
    return out
