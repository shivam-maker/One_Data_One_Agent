"""Behavioural tests: late/duplicate triggers, conflicting sources, tool failures, corrected goal rules, and the
safety checks around them. Each test builds a fresh agent system with only the viewers it needs."""
from datetime import timedelta

from conftest import agent_user, make_event

from oua.judge import judge_all
from oua.timeutil import iso, parse
from oua.verifier import CheckResult

T = "2026-10-01T09:00:00.000Z"  # 14:30 IST


def _never_paid_with_profile(base):
    pack = base["pack"]
    return agent_user(base, lambda u: len(pack.profiles.get(u, [])) == 1
                      and pack.profiles[u][0]["subscription_status"] == "never"
                      and pack.profiles[u][0]["preferred_dialect"] == "d1")


# ---- late and duplicate triggers ---------------------------------------------------------------------------------
def test_duplicate_trigger_is_idempotent(base, make_system):
    u = _never_paid_with_profile(base)
    sys_ = make_system([u])
    trig = make_event(u, "app_open", T, "d1")
    d1 = sys_.handle_trigger(dict(trig))
    d2 = sys_.handle_trigger(dict(trig, deliver_at=iso(parse(T) + timedelta(minutes=4))))
    assert d1["status"] == "PUBLISHED"
    assert d2["status"] == "DUPLICATE_IGNORED" and d2["original_decision_id"] == d1["decision_id"]
    assert len(sys_.state.decisions(u)) == 1
    assert len([a for a in sys_.state.actions(u) if a["type"] == "home_page"]) == 1
    assert sys_.state.ledger_get(trig["event_id"])["deliveries"] == 2


def test_offline_buffered_trigger_takes_no_action_and_commits_no_goal(base, make_system):
    u = _never_paid_with_profile(base)
    sys_ = make_system([u])
    # Buffered for 2 days on the phone, and the phone clock is also 3 hours fast.
    trig = make_event(u, "app_open", T, "d1", buffered_s=2 * 86400, clock_offset_s=3 * 3600)
    d = sys_.handle_trigger(trig)
    assert d["trigger"]["event_age_s"] == 2 * 86400  # age is immune to the wrong clock
    assert d["action"] == "NO_ACTION" and "buffered" in d["no_action_reason"]
    assert d["goal"] is None and sys_.state.actions(u) == []


def test_wrong_phone_clock_alone_does_not_make_a_trigger_stale(base, make_system):
    u = _never_paid_with_profile(base)
    sys_ = make_system([u])
    d = sys_.handle_trigger(make_event(u, "app_open", T, "d1", clock_offset_s=-9 * 3600))
    assert d["trigger"]["event_age_s"] == 0 and d["trigger"]["device_clock_suspect"]
    assert d["status"] == "PUBLISHED"


def test_second_trigger_in_same_session_does_not_republish(base, make_system):
    u = _never_paid_with_profile(base)
    sys_ = make_system([u])
    login = make_event(u, "login_success", T, "d1", session="s-1")
    opened = make_event(u, "app_open", iso(parse(T) + timedelta(seconds=2)), "d1", session="s-1")
    assert sys_.handle_trigger(login)["status"] == "PUBLISHED"
    d = sys_.handle_trigger(opened)
    assert d["action"] == "NO_ACTION" and "already acted" in d["no_action_reason"]


# ---- conflicting sources -----------------------------------------------------------------------------------------
def test_three_dialects_uses_app_setting_and_records_conflicts(base, make_system):
    u = _never_paid_with_profile(base)  # profile says d1
    sys_ = make_system([u])
    trig = make_event(u, "app_open", T, "d2", props={"deeplink": "ott://c/cmp032/d5/title/T1901"})
    d = sys_.handle_trigger(trig)
    assert d["dialect"]["used"] == "d2" and d["dialect"]["source"] == "app_setting"
    assert "profile:d1" in d["dialect"]["conflicts"] and "campaign_link:d5" in d["dialect"]["conflicts"]
    page = sys_.state.actions(u)[0]
    titles = [page["hero_title_id"]] + [t for r in page["rows"] for t in r["title_ids"]]
    assert all(sys_.pack.titles[t].dialect == "d2" for t in titles)
    assert all(len(r["title_ids"]) >= 3 for r in page["rows"])


def test_unsupported_app_dialect_gets_no_page(base, make_system):
    u = _never_paid_with_profile(base)
    sys_ = make_system([u])
    d = sys_.handle_trigger(make_event(u, "app_open", T, "d7"))
    assert d["action"] == "NO_ACTION" and "unsupported dialect" in d["no_action_reason"]


def test_duplicate_profile_rows_never_after_paid_is_a_bad_merge(base, make_system):
    # U-04215 has an older 'lapsed' row and a newer 'never' row.
    sys_ = make_system(["U-04215"])
    from oua.facts import build_facts

    fs = build_facts(sys_.pack, "U-04215", None, sys_.events.history("U-04215", parse(T)), parse(T),
                     sys_.history_start, 30)
    st = fs.fact("subscription_status")
    assert st.value == "lapsed"
    assert any("subscription_status" in c for c in fs.fact("profile_row").conflicts)
    assert sys_.registry.eligibility("G05", fs, parse(T)).eligible is False


def test_later_opt_out_event_overrides_profile_consent(base, make_system):
    u = agent_user(base, lambda x: base["pack"].profiles.get(x) and
                   base["pack"].profiles[x][0]["notifications_opt_in"] == "true" and len(base["pack"].profiles[x]) == 1)
    opt = make_event(u, "notification_opt_out", "2026-09-30T10:00:00.000Z", props={"channel": "push"})
    sys_ = make_system([u], [opt])
    from oua.facts import build_facts

    fs = build_facts(sys_.pack, u, None, sys_.events.history(u, parse(T)), parse(T), sys_.history_start, 30)
    assert fs.get("consent_push") is False and fs.fact("consent_push").conflicts


# ---- tool / model failures ---------------------------------------------------------------------------------------
REVIEWED = "U-01991"  # several eligible goals, so the agent asks the model for a review


def test_model_timeout_falls_back_within_budget(base, make_system):
    u = REVIEWED
    trig = make_event(u, "app_open", T, "d3")
    sys_ = make_system([u], faults={trig["event_id"]: {"fault": "timeout", "seconds": 45}})
    d = sys_.handle_trigger(trig)
    review = [m for m in d["model"] if m["task"] == "goal_review"]
    assert review and not review[0]["ok"] and "timeout" in review[0]["error"]
    assert d["status"] == "PUBLISHED" and d["latency_ms"] < 60_000
    assert d["budget"]["model_calls"] <= d["budget"]["max_model_calls"]


def test_malformed_model_output_is_ignored(base, make_system):
    u = REVIEWED
    trig = make_event(u, "app_open", T, "d3")
    sys_ = make_system([u], faults={trig["event_id"]: {"fault": "malformed_output",
                                                       "model_output": "{\"goal_id\": \"G02\", \"eligible\": tr"}})
    d = sys_.handle_trigger(trig)
    assert "malformed" in d["model"][0]["error"] and d["status"] == "PUBLISHED"


def test_unsupported_model_claim_is_rejected(base, make_system):
    u = "U-01991"  # profile: never paid; the fault claims an active trial
    trig = make_event(u, "app_open", T, "d3")
    claim = {"goal_id": "G04", "eligible": True, "confidence": 0.97,
             "reason": "The user is on an active trial that ends tomorrow (confirmed in billing)."}
    sys_ = make_system([u], faults={trig["event_id"]: {"fault": "unsupported_claim", "model_output": claim}})
    d = sys_.handle_trigger(trig)
    assert d["goal"]["id"] != "G04"
    assert d["model"][0]["used_as"].startswith("REJECTED")
    assert any(c["check"] == "model_claim" for c in d["checks_extra"])
    assert sys_.state.audit_log(u, "model_claim_rejected")


def test_model_provider_outage_window(base, make_system):
    u = REVIEWED
    sys_ = make_system([u])
    sys_.on_control({"at": "2026-10-01T08:55:00.000Z", "type": "dependency_unavailable",
                     "dependency": "model_provider", "until": "2026-10-01T09:05:00.000Z"})
    d = sys_.handle_trigger(make_event(u, "app_open", T, "d3"))
    assert d["status"] == "PUBLISHED"
    assert d["model"] and all(not m["ok"] and "503" in m["error"] for m in d["model"])


def test_data_store_outage_without_cache_commits_no_goal(base, make_system):
    u = _never_paid_with_profile(base)
    sys_ = make_system([u])
    sys_.on_control({"at": "2026-10-01T08:55:00.000Z", "type": "dependency_unavailable",
                     "dependency": "user_data_store", "until": "2026-10-01T09:05:00.000Z"})
    d = sys_.handle_trigger(make_event(u, "app_open", T, "d1"))
    assert d["degraded"] and d["facts_source"] == "trigger_and_snapshot_only"
    assert d["goal"] is None and "data store" in d["no_goal_reason"]
    # Smaller safe action: a generic in-dialect page, no messages planned.
    assert d["action"] in ("HOME_PAGE", "NO_ACTION") and not d["plan"]


def test_data_store_outage_with_cache_continues_existing_goal(base, make_system):
    u = _never_paid_with_profile(base)
    sys_ = make_system([u])
    d1 = sys_.handle_trigger(make_event(u, "app_open", "2026-10-01T06:00:00.000Z", "d1"))
    sys_.on_control({"at": "2026-10-01T08:55:00.000Z", "type": "dependency_unavailable",
                     "dependency": "user_data_store", "until": "2026-10-01T09:05:00.000Z"})
    d2 = sys_.handle_trigger(make_event(u, "app_open", T, "d1"))
    assert d2["facts_source"] == "agent_memory_cache"
    assert d2["goal"]["instance_id"] == d1["goal"]["instance_id"] and d2["goal_status"] == "continued"


# ---- corrected goal rule -----------------------------------------------------------------------------------------
def test_goal_rule_correction_touches_only_open_goals_of_that_goal(base, make_system):
    pack = base["pack"]
    new = agent_user(base, lambda x: not pack.profiles.get(x) and x not in base["by_user"])
    other = _never_paid_with_profile(base)
    signup = make_event(new, "signup_completed", "2026-09-30T12:00:00.000Z", "d1", props={"method": "phone"})
    played = make_event(new, "play_started", "2026-09-30T12:05:00.000Z", "d1",
                        props={"title_id": "T0003", "episode_id": "raloja-d1-s1e1"})
    sys_ = make_system([other], [signup, played])
    dn = sys_.handle_trigger(make_event(new, "app_open", "2026-10-01T05:00:00.000Z", "d1"))
    do = sys_.handle_trigger(make_event(other, "app_open", "2026-10-01T05:00:00.000Z", "d1"))
    assert dn["goal"]["id"] == "G03" and do["goal"]["id"] != "G03"
    before_other = sys_.state.goals(user_id=other)[0]

    # Next day (IST) the viewer only opens the app: that met the old rule, not the corrected one.
    reopen = make_event(new, "app_open", "2026-10-01T20:00:00.000Z", "d1")  # 01:30 IST on Oct 2
    sys_.observe(reopen)
    assert sys_.state.goals(user_id=new)[0]["status"] == "achieved_provisional"

    s = sys_.on_control({"at": "2026-10-01T21:00:00.000Z", "type": "goal_rule_corrected", "goal_id": "G03",
                         "new_success": {"event": "play_started",
                                         "condition": "on a later IST calendar day within the window"},
                         "old_success": {"event": "app_open"}})
    assert s["affected_viewers"] == [new]
    g = sys_.state.goals(user_id=new)[0]
    assert g["rule_version"] == "G03-r2" and g["status"] == "open"  # provisional success withdrawn
    assert sys_.state.goals(user_id=other)[0] == before_other  # untouched

    later_play = make_event(new, "play_started", "2026-10-02T05:00:00.000Z", "d1",
                            props={"title_id": "T0003", "episode_id": "raloja-d1-s1e1"})
    ev, ver = sys_.registry.success_event(g, [__import__("oua.events", fromlist=["normalize"]).normalize(later_play)])
    assert ev is not None and ver == "G03-r2"


# ---- safety ------------------------------------------------------------------------------------------------------
def test_holdout_viewer_gets_no_action_but_a_shadow_goal(base, make_system):
    u = "U-00001"
    assert base["pack"].arms[u] == "holdout"
    sys_ = make_system([u])
    d = sys_.handle_trigger(make_event(u, "app_open", T, "d1"))
    assert d["status"] == "HOLDOUT_NO_ACTION" and sys_.state.actions(u) == []
    assert d["model"] == []
    if d["goal"]:
        assert sys_.state.goals(user_id=u)[0]["arm"] == "holdout"


def test_injected_catalogue_text_is_quarantined_and_never_sent_to_model(base, make_system):
    pack = base["pack"]
    assert pack.titles["T0002"].injection and pack.titles["T0522"].injection
    seen_payloads = []

    class Spy:
        def complete(self, task, payload, trigger_event_id):
            seen_payloads.append(payload)
            from oua.llm import MockProvider
            return MockProvider().complete(task, payload, trigger_event_id)

    u = _never_paid_with_profile(base)
    sys_ = make_system([u], provider=Spy())
    sys_.handle_trigger(make_event(u, "app_open", T, "d1"))
    page = sys_.state.actions(u)[0]
    ids = {page["hero_title_id"]} | {t for r in page["rows"] for t in r["title_ids"]}
    assert "T0002" not in ids
    blob = str(seen_payloads).lower()
    assert "system note" not in blob and "description" not in blob


def test_message_rules_consent_quiet_hours_and_cap(base, make_system):
    u = _never_paid_with_profile(base)
    sys_ = make_system([u])
    from oua.facts import build_facts

    fs = build_facts(sys_.pack, u, None, sys_.events.history(u, parse(T)), parse(T), sys_.history_start, 30)
    tid = next(t.title_id for t in sys_.pack.titles_by_dialect["d1"]
               if sys_.pack.title_valid(t.title_id, "d1", parse(T))[0])
    name = sys_.pack.titles[tid].name
    msg = {"type": "push", "title_id": tid, "deeplink": f"ott://title/{tid}",
           "text": {"title": f"{name} ab dekhiye", "body": f"{name} dekhiye"},
           "_send_at": parse("2026-10-01T17:00:00.000Z")}  # 22:30 IST
    fs._f["consent_push"].value = True
    fs._f["messages_today"].value = 0
    r = sys_.verifier.verify_message(u, msg, fs, [])
    assert "quiet_hours" in [f["check"] for f in r.failed]
    msg["_send_at"] = parse("2026-10-01T13:30:00.000Z")  # 19:00 IST
    r = sys_.verifier.verify_message(u, msg, fs, [{"ist_day": "2026-10-01"}])
    assert [f["check"] for f in r.failed] == ["frequency_cap"]
    fs._f["consent_push"].value = False
    msg["text"]["title"] = "Last chance! " + name
    r = sys_.verifier.verify_message(u, msg, fs, [])
    failed = {f["check"] for f in r.failed}
    assert {"consent", "copy_rules"} <= failed


def test_purchase_right_after_decision_revises_plan_and_is_not_credited(base, make_system, tmp_path):
    import json

    pack = base["pack"]
    # Find a never-paid viewer whose committed goal is G05 (first paid plan).
    for u in [x for x, a in sorted(pack.arms.items()) if a == "agent" and len(pack.profiles.get(x, [])) == 1
              and pack.profiles[x][0]["subscription_status"] == "never"][:80]:
        sys_ = make_system([u])
        d = sys_.handle_trigger(make_event(u, "app_open", T, pack.profiles[u][0]["preferred_dialect"], session="s-9"))
        if d["goal"] and d["goal"]["id"] == "G05" and d["action"] == "HOME_PAGE":
            break
    else:
        raise AssertionError("no G05 viewer found")
    dialect = d["dialect"]["used"]
    buy = make_event(u, "purchase_completed", iso(parse(T) + timedelta(minutes=2)), dialect, session="s-9",
                     props={"plan_id": "P_MONTH", "price_inr": 149, "is_trial": False})
    sys_.observe(buy)
    g = sys_.state.goals(user_id=u)[0]
    assert g["status"] == "achieved_provisional"
    assert sys_.state.load_viewer(u)[0]["plan"] is None  # pending nudges cancelled
    assert sys_.state.audit_log(u, "plan_revised")
    # Next visit: the snapshot still says 'never', but the newer purchase event wins and the plan is revised.
    d2 = sys_.handle_trigger(make_event(u, "app_open", iso(parse(T) + timedelta(hours=3)), dialect))
    assert d2["plan_revision"].startswith("previous goal G05 reached")
    assert d2["dialect"] and (d2["goal"] is None or d2["goal"]["id"] in ("G08", "G09", "G07", "G02"))
    st = next(c for c in d2["conflicts"] if c["fact"] == "subscription_status")
    assert st["used"] == "active" and "profile:never" in st["conflicts"]
    # Judging: a purchase 2 minutes after the decision was already in flight, so it is not credited.
    sim = tmp_path / "sim"
    sim.mkdir()
    page = sys_.state.actions(u)[0]
    shown = make_event(u, "home_page_shown", iso(parse(T) + timedelta(seconds=10)), dialect,
                       props={"personalised": True, "page_action_id": page["action_id"]})
    (sim / "outcome_events.jsonl").write_text("\n".join(json.dumps(e) for e in (shown, buy)))
    out = judge_all(sys_, sim, tmp_path / "outcomes.jsonl")
    o = next(x for x in out if x["goal_instance_id"] == g["goal_instance_id"])
    assert o["rule_met"] and o["verdict"] == "inconclusive" and o["attribution"] == "in_flight"


def test_title_withdrawn_republishes_affected_pages_only(base, make_system):
    pack = base["pack"]
    u = _never_paid_with_profile(base)
    sys_ = make_system([u])
    sys_.handle_trigger(make_event(u, "app_open", T, "d1"))
    page = sys_.state.actions(u)[0]
    victim = page["rows"][0]["title_ids"][0]
    s = sys_.on_control({"at": iso(parse(T) + timedelta(minutes=10)), "type": "title_withdrawn",
                         "title_id": victim, "reason": "licence ended"})
    assert s["affected_viewers"] == [u]
    acts = sys_.state.actions(u)
    assert acts[0]["status"] == "superseded"
    new = acts[-1]
    assert victim not in {new["hero_title_id"]} | {t for r in new["rows"] for t in r["title_ids"]}
    assert pack.withdrawn == {}  # the shared pack in other tests is unaffected


def test_check_result_records_failures():
    r = CheckResult()
    r.check("a", True)
    r.check("b", False, "why")
    assert not r.ok and r.to_dict() == {"passed": ["a"], "failed": [{"check": "b", "detail": "why"}]}
