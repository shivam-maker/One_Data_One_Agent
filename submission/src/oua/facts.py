"""Step 1, "understand the viewer": assemble facts with source, time, freshness and explicit conflict resolution.

Precedence rules per fact (never "newest/largest source wins" by default):

  dialect the app will show   trigger app setting (if a real dialect) > nothing. Profile and campaign link are
                              recorded as conflicts but never used for a home page: the app only requests the page
                              for its current setting.
  dialect for messages        latest valid app setting seen in events > profile preferred_dialect.
  subscription status         lifecycle events (purchase/renew/lapse) received after the profile row > profile.
                              Duplicate profile rows: "ever paid" is monotonic, so a newer "never" row that
                              contradicts an older paid row is treated as a bad merge.
  consent                     profile opt-in AND no later notification_opt_out event. No profile => no consent.
  signup time                 profile signup_at > signup_completed event (new users have no profile row).
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .pack import VALID_DIALECTS, Pack
from .timeutil import iso, ist_day, parse

PAID_STATES = {"active", "lapsed", "trial_active"}


@dataclass
class Fact:
    name: str
    value: Any
    source: str
    observed_at: str | None
    age_s: int | None
    status: str = "ok"  # ok | conflict_resolved | unknown | assumed
    conflicts: list[str] = field(default_factory=list)
    resolution: str | None = None
    refs: list[str] = field(default_factory=list)


class Facts:
    def __init__(self, now: datetime):
        self.now = now
        self._f: dict[str, Fact] = {}

    def add(self, name, value, source, observed_at: datetime | None, status="ok", conflicts=None,
            resolution=None, refs=None) -> Fact:
        age = int((self.now - observed_at).total_seconds()) if observed_at else None
        f = Fact(name, value, source, iso(observed_at), age, status, conflicts or [], resolution, refs or [])
        self._f[name] = f
        return f

    def get(self, name, default=None):
        f = self._f.get(name)
        return f.value if f else default

    def fact(self, name) -> Fact | None:
        return self._f.get(name)

    def conflicts(self) -> list[dict]:
        return [{"fact": f.name, "used": f.value, "source": f.source, "conflicts": f.conflicts,
                 "resolution": f.resolution} for f in self._f.values() if f.conflicts]

    def to_dict(self) -> dict:
        return {k: asdict(v) for k, v in self._f.items()}

    @classmethod
    def from_dict(cls, now: datetime, d: dict) -> "Facts":
        fs = cls(now)
        for k, v in d.items():
            fs._f[k] = Fact(**v)
        return fs


def _deeplink_dialect(trigger: dict) -> str | None:
    dl = (trigger.get("properties") or {}).get("deeplink") or ""
    parts = dl.split("/")
    # ott://c/<campaign>/<dialect>/title/<title_id>
    if dl.startswith("ott://c/") and len(parts) >= 5:
        return parts[4] if parts[4] else None
    return None


def resolve_profile(rows: list[dict]) -> tuple[dict | None, list[str], str | None]:
    """Picks one profile row from possibly-duplicated rows and explains the choice."""
    if not rows:
        return None, [], None
    rows = sorted(rows, key=lambda r: r["updated_at"])
    newest = rows[-1]
    if len(rows) == 1:
        return newest, [], None
    conflicts = []
    for col in ("subscription_status", "preferred_dialect", "notifications_opt_in", "whatsapp_opt_in", "signup_at"):
        vals = {r[col] for r in rows}
        if len(vals) > 1:
            conflicts.append(f"profile_duplicate:{col}=" + "|".join(sorted(vals)))
    chosen = dict(newest)
    resolution = "newest profile row by updated_at"
    paid_rows = [r for r in rows if r["subscription_status"] in PAID_STATES]
    if newest["subscription_status"] == "never" and paid_rows:
        # A user who has paid can never become "never" again: the newer row is a bad merge for this column.
        chosen["subscription_status"] = paid_rows[-1]["subscription_status"]
        chosen["_status_from_row_updated_at"] = paid_rows[-1]["updated_at"]
        resolution = ("newest row for most columns, but subscription_status taken from the latest paid-state row "
                      "because 'never' cannot follow a paid state")
    # Consent: if duplicates disagree, the conservative value (no consent) wins.
    for col in ("notifications_opt_in", "whatsapp_opt_in"):
        if len({r[col] for r in rows}) > 1:
            chosen[col] = "false"
            resolution += f"; {col} disagrees across rows so consent is treated as not given"
    return chosen, conflicts, resolution


def build_facts(pack: Pack, user_id: str, trigger: dict | None, history: list[dict], now: datetime,
                history_start: datetime, recent_lapse_days: int, own_messages: list[dict] | None = None) -> Facts:
    fs = Facts(now)
    snapshot_at = None

    # ---- profile -------------------------------------------------------------------------------------------
    prof, prof_conflicts, prof_resolution = resolve_profile(pack.profiles.get(user_id, []))
    if prof:
        snapshot_at = parse(prof["snapshot_at"])
        fs.add("profile_row", True, f"profile_snapshot:{user_id}", parse(prof["updated_at"]),
               status="conflict_resolved" if prof_conflicts else "ok", conflicts=prof_conflicts,
               resolution=prof_resolution, refs=[f"profile:{user_id}@{prof['updated_at']}"])
    else:
        fs.add("profile_row", False, "profile_snapshot", None, status="unknown",
               resolution="no profile row (brand-new user or missing from the daily export)")

    # ---- signup --------------------------------------------------------------------------------------------
    signup_events = [e for e in history if e["event"] == "signup_completed"]
    if prof and prof.get("signup_at"):
        sv = parse(prof["signup_at"])
        conf = [f"event:{e['event_id']}:{iso(e['_true_at'])}" for e in signup_events
                if abs((e["_true_at"] - sv).total_seconds()) > 86400]
        fs.add("signup_at", iso(sv), "profile_snapshot", parse(prof["updated_at"]), conflicts=conf,
               status="conflict_resolved" if conf else "ok",
               resolution="profile signup_at preferred over device-clock events" if conf else None,
               refs=[f"profile:{user_id}"])
    elif signup_events:
        e = signup_events[0]
        fs.add("signup_at", iso(e["_true_at"]), "event:signup_completed", e["_received"],
               refs=[f"event:{e['event_id']}"])
    else:
        fs.add("signup_at", None, "none", None, status="unknown")

    # ---- dialect -------------------------------------------------------------------------------------------
    app_now = (trigger or {}).get("app_dialect")
    last_valid_app, last_valid_ref, last_valid_at = None, None, None
    for e in history:
        d = e.get("app_dialect")
        if e["event"] == "app_dialect_changed":
            d = (e.get("properties") or {}).get("to") or d
        if d in VALID_DIALECTS:
            last_valid_app, last_valid_ref, last_valid_at = d, f"event:{e['event_id']}", e["_received"]
    if trigger and app_now in VALID_DIALECTS:
        last_valid_app, last_valid_ref, last_valid_at = app_now, f"event:{trigger['event_id']}", trigger["_received"]
    profile_d = prof.get("preferred_dialect") if prof else None
    link_d = _deeplink_dialect(trigger or {})
    conflicts = []
    if profile_d and profile_d != app_now:
        conflicts.append(f"profile:{profile_d}")
    if link_d and link_d != app_now:
        conflicts.append(f"campaign_link:{link_d}")
    if trigger and app_now in VALID_DIALECTS:
        fs.add("dialect_home", app_now, "app_setting", trigger["_received"], conflicts=conflicts,
               status="conflict_resolved" if conflicts else "ok",
               resolution=("the app requests the home page for its current setting; profile is a day stale and a "
                           "campaign link describes the ad, not the app") if conflicts else None,
               refs=[f"event:{trigger['event_id']}"])
    else:
        fs.add("dialect_home", None, "app_setting", trigger["_received"] if trigger else None, status="unknown",
               conflicts=conflicts + ([f"app_setting:{app_now}"] if trigger else []),
               resolution=(f"app reports unsupported dialect {app_now!r}; a page in any other dialect would not be "
                           "shown, so no home page can be published") if trigger else "no live trigger")
    if last_valid_app:
        fs.add("dialect_messages", last_valid_app, "app_setting", last_valid_at,
               conflicts=[f"profile:{profile_d}"] if profile_d and profile_d != last_valid_app else [],
               status="conflict_resolved" if profile_d and profile_d != last_valid_app else "ok",
               resolution="latest valid app setting beats the stale profile" if profile_d and profile_d != last_valid_app else None,
               refs=[last_valid_ref])
    elif profile_d in VALID_DIALECTS:
        fs.add("dialect_messages", profile_d, "profile_snapshot", parse(prof["updated_at"]), status="assumed",
               resolution="no valid app setting seen; falling back to profile preferred_dialect",
               refs=[f"profile:{user_id}"])
    else:
        fs.add("dialect_messages", None, "none", None, status="unknown")
    lang = (trigger or {}).get("app_language") or (prof.get("preferred_language") if prof else None)
    fs.add("language", lang, "app_setting" if (trigger or {}).get("app_language") else "profile_snapshot",
           trigger["_received"] if trigger else None)

    # ---- subscription status -------------------------------------------------------------------------------
    life = [e for e in history if e["event"] in ("purchase_completed", "subscription_renewed", "subscription_lapsed")]
    ever_paid_events = [e for e in life if e["event"] == "subscription_renewed" or (
        e["event"] == "purchase_completed" and not (e.get("properties") or {}).get("is_trial"))]
    prof_status = prof.get("subscription_status") if prof else None
    prof_time = parse(prof.get("_status_from_row_updated_at") or prof["updated_at"]) if prof else None
    if life and (prof_time is None or life[-1]["_received"] > prof_time):
        last = life[-1]
        if last["event"] == "subscription_lapsed":
            st = "lapsed"
        elif last["event"] == "purchase_completed" and (last.get("properties") or {}).get("is_trial"):
            st = "trial_active"
        else:
            st = "active"
        conf = [f"profile:{prof_status}"] if prof_status and prof_status != st else []
        fs.add("subscription_status", st, f"event:{last['event']}", last["_received"], conflicts=conf,
               status="conflict_resolved" if conf else "ok",
               resolution="lifecycle event received after the profile row supersedes it" if conf else None,
               refs=[f"event:{last['event_id']}"])
    elif prof_status:
        conf = []
        if prof_status == "never" and ever_paid_events:
            conf.append(f"event:{ever_paid_events[-1]['event_id']}:paid")
        fs.add("subscription_status", prof_status, "profile_snapshot", prof_time, conflicts=conf,
               status="conflict_resolved" if conf else "ok",
               resolution="profile row is newer than any lifecycle event" if conf else None,
               refs=[f"profile:{user_id}"])
    else:
        fs.add("subscription_status", None, "none", None, status="unknown",
               resolution="no profile row and no lifecycle events")
    fs.add("ever_paid", bool(ever_paid_events) or prof_status in ("active", "lapsed"),
           "events+profile", ever_paid_events[-1]["_received"] if ever_paid_events else prof_time)
    lapse_events = [e for e in history if e["event"] == "subscription_lapsed"]
    recent_lapse = None
    if lapse_events and now - lapse_events[-1]["_true_at"] <= timedelta(days=recent_lapse_days):
        recent_lapse = lapse_events[-1]
    elif prof and prof.get("plan_ends_at"):
        pe = parse(prof["plan_ends_at"])
        if pe and timedelta(0) <= now - pe <= timedelta(days=recent_lapse_days):
            recent_lapse = {"event_id": f"profile:{user_id}:plan_ends_at", "_true_at": pe, "_received": pe}
    fs.add("recent_lapse", bool(recent_lapse), "event:subscription_lapsed" if recent_lapse else "none",
           recent_lapse["_received"] if recent_lapse else None,
           refs=[f"event:{recent_lapse['event_id']}"] if recent_lapse else [],
           resolution=f"'recently' defined as within {recent_lapse_days} days (goal rule is underspecified)")
    if prof and prof.get("plan_ends_at"):
        fs.add("plan_ends_at", prof["plan_ends_at"], "profile_snapshot", parse(prof["updated_at"]))

    # ---- consent -------------------------------------------------------------------------------------------
    opt_outs = defaultdict(list)
    for e in history:
        if e["event"] == "notification_opt_out":
            opt_outs[(e.get("properties") or {}).get("channel")].append(e)
    for ch, col in (("push", "notifications_opt_in"), ("whatsapp", "whatsapp_opt_in")):
        if not prof:
            fs.add(f"consent_{ch}", False, "default_deny", None, status="unknown",
                   resolution="no profile row: consent cannot be shown, so it is treated as absent")
            continue
        base = prof.get(col) == "true"
        if opt_outs.get(ch):
            e = opt_outs[ch][-1]
            fs.add(f"consent_{ch}", False, "event:notification_opt_out", e["_received"],
                   conflicts=[f"profile:{col}={prof.get(col)}"] if base else [],
                   status="conflict_resolved" if base else "ok",
                   resolution="a later opt-out event overrides the snapshot", refs=[f"event:{e['event_id']}"])
        else:
            fs.add(f"consent_{ch}", base, "profile_snapshot", parse(prof["updated_at"]), refs=[f"profile:{user_id}"])

    # ---- engagement ----------------------------------------------------------------------------------------
    cur_session = (trigger or {}).get("session_id")
    plays = [e for e in history if e["event"] == "play_started"]
    completes = [e for e in history if e["event"] == "episode_completed"]
    opens_before = [e for e in history if e["event"] in ("app_open", "login_success")
                    and e.get("session_id") != cur_session and e["_true_at"] < now]
    fs.add("has_played", bool(plays), "events", plays[-1]["_received"] if plays else None,
           refs=[f"event:{plays[-1]['event_id']}"] if plays else [])
    fs.add("has_completed", bool(completes), "events", completes[-1]["_received"] if completes else None,
           refs=[f"event:{completes[-1]['event_id']}"] if completes else [])
    fs.add("history_covers_lifetime",
           bool(fs.get("signup_at")) and parse(fs.get("signup_at")) >= history_start, "profile+events", None,
           resolution=f"event history starts {iso(history_start)}; earlier behaviour is unknown")
    fs.add("last_open_before", iso(opens_before[-1]["_true_at"]) if opens_before else None, "events",
           opens_before[-1]["_received"] if opens_before else None,
           refs=[f"event:{opens_before[-1]['event_id']}"] if opens_before else [])
    days14 = {ist_day(e["_true_at"]) for e in plays if now - e["_true_at"] <= timedelta(days=14)}
    fs.add("play_days_14d", len(days14), "events", None)
    fs.add("paywall_views_14d", sum(1 for e in history if e["event"] == "paywall_viewed"
                                    and now - e["_true_at"] <= timedelta(days=14)), "events", None)
    genres = defaultdict(int)
    watched_slugs = set()
    progress: dict[str, dict] = {}
    for e in plays + completes:
        t = pack.titles.get((e.get("properties") or {}).get("title_id"))
        if not t:
            continue
        watched_slugs.add(t.slug)
        if e["event"] == "play_started":
            for g in t.genres:
                genres[g] += 1
        p = progress.setdefault(t.slug, {"slug": t.slug, "title_id": t.title_id, "type": t.content_type,
                                         "max_ep_idx": -1, "last_play": None, "ref": None})
        ep_id = (e.get("properties") or {}).get("episode_id")
        eps = pack.episodes.get(t.slug, [])
        idx = next((i for i, x in enumerate(eps) if x[2] == ep_id), -1)
        if e["event"] == "episode_completed":
            p["max_ep_idx"] = max(p["max_ep_idx"], idx)
        if e["event"] == "play_started" and (p["last_play"] is None or e["_true_at"] > p["last_play"]):
            p["last_play"] = e["_true_at"]
            p["ref"] = f"event:{e['event_id']}"
    unfinished = []
    for slug, p in progress.items():
        eps = pack.episodes.get(slug, [])
        # "Started" a series is read as having finished at least one episode of it and played it in the last 30 days;
        # a single abandoned sample of episode 1 is a rejection, not an unfinished series (G07 rule is underspecified).
        if p["type"] in ("show", "micro_drama") and p["last_play"] and len(eps) > 1 and \
                0 <= p["max_ep_idx"] < len(eps) - 1 and now - p["last_play"] <= timedelta(days=30):
            unfinished.append({"slug": slug, "title_id": p["title_id"], "next_ep_idx": p["max_ep_idx"] + 1,
                               "episodes": len(eps), "last_play": iso(p["last_play"]),
                               "days_since": round((now - p["last_play"]).total_seconds() / 86400, 2),
                               "ref": p["ref"]})
    unfinished.sort(key=lambda x: x["last_play"], reverse=True)
    fs.add("unfinished_series", unfinished, "events+episodes", None)
    fs.add("watched_slugs", sorted(watched_slugs), "events", None)
    fs.add("top_genres", [g for g, _ in sorted(genres.items(), key=lambda kv: -kv[1])[:3]], "events", None)

    # Messages already sent today (platform deliveries + our own published messages).
    today = ist_day(now)
    delivered = [e for e in history if e["event"] == "message_delivered" and ist_day(e["_true_at"]) == today]
    own = [m for m in (own_messages or []) if m.get("ist_day") == str(today)]
    fs.add("messages_today", len(delivered) + len(own), "events+agent_memory", None,
           refs=[f"event:{e['event_id']}" for e in delivered] + [f"action:{m['action_id']}" for m in own])
    push_fail = [e for e in history if e["event"] == "message_failed"
                 and (e.get("properties") or {}).get("reason") == "token_invalid"]
    fs.add("push_token_invalid", bool(push_fail), "events", None)
    if snapshot_at:
        fs.add("profile_snapshot_age_h", round((now - snapshot_at).total_seconds() / 3600, 1), "profile_snapshot",
               snapshot_at)
    return fs
