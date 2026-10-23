"""API smoke test and an end-to-end replay with invariant checks over every published action."""
import json

import pytest
from conftest import make_event

from oua.timeutil import in_quiet_hours, parse


def test_api_trigger_state_and_explain(settings, tmp_path):
    from fastapi.testclient import TestClient

    from oua.api import create_app

    client = TestClient(create_app(settings, tmp_path / "api.db", reset=True))
    u = "U-01991"
    trig = make_event(u, "app_open", "2026-10-01T09:00:00.000Z", "d3")
    r = client.post("/v1/triggers", json=trig)
    assert r.status_code == 200 and r.json()["status"] == "PUBLISHED"
    assert client.post("/v1/triggers", json=trig).json()["status"] == "DUPLICATE_IGNORED"
    v = client.get(f"/v1/viewers/{u}").json()
    assert v["active_goal"] and v["decision_ids"] == [r.json()["decision_id"]]
    ex = client.get(f"/v1/viewers/{u}/explain", params={"at": "2026-10-01T09:30:00Z"}).json()
    assert ex["knew"]["facts_source"] == "live" and len(ex["decided"]) == 1
    assert client.get("/v1/decisions/" + r.json()["decision_id"]).status_code == 200
    assert client.post("/v1/triggers", json=dict(trig, event="search")).status_code == 422


def test_dashboard_and_demo_seed(settings, tmp_path):
    from fastapi.testclient import TestClient

    from oua.api import create_app

    client = TestClient(create_app(settings, tmp_path / "ui.db", reset=True))
    page = client.get("/")
    assert page.status_code == 200 and "Load demo data" in page.text
    r = client.post("/v1/demo/seed", params={"replay_triggers": 20}).json()
    by = {s["scenario"]: s for s in r["scenarios"]}
    assert by["Same trigger delivered again"]["status"] == "DUPLICATE_IGNORED"
    assert by["Holdout viewer"]["status"] == "HOLDOUT_NO_ACTION"
    assert by["App set to unsupported dialect d7"]["status"] == "NO_ACTION"
    summary = client.get("/v1/summary").json()
    assert summary["decisions"] > 0 and summary["model_claims_rejected"] == 1
    rows = client.get("/v1/decisions", params={"status": "PUBLISHED"}).json()
    assert rows and all(x["status"] == "PUBLISHED" for x in rows)
    assert client.get("/v1/titles", params={"ids": "T0001"}).json()["T0001"]["dialect"] == "d1"
    assert client.post("/v1/admin/reset").json()["ok"] and client.get("/v1/summary").json()["decisions"] == 0


@pytest.fixture(scope="module")
def replay_run(tmp_path_factory):
    from oua.config import Settings
    from oua.replay import run_replay

    out = tmp_path_factory.mktemp("run")
    r = run_replay(Settings(), out, log=lambda *a: None)
    return out, r


def test_replay_invariants(replay_run):
    out, r = replay_run
    system = r["system"]
    pack = system.pack
    actions = [json.loads(l) for l in open(out / "actions.jsonl", encoding="utf-8")]
    decisions = [json.loads(l) for l in open(out / "decisions.jsonl", encoding="utf-8")]
    assert actions and decisions
    for a in actions:
        assert pack.arms[a["user_id"]] == "agent", "holdout viewers must never receive an action"
        at = parse(a.get("send_at") or a["published_at"])
        if a["type"] == "home_page":
            ids = [a["hero_title_id"]] if a["hero_title_id"] else []
            for row in a["rows"]:
                assert 3 <= len(row["title_ids"]) <= 10
                ids += row["title_ids"]
            slugs = [pack.titles[t].slug for t in ids]
            assert len(slugs) == len(set(slugs))
            for t in ids:
                ti = pack.titles[t]
                assert ti.dialect == a["dialect"] and not ti.injection
                assert ti.available_from <= parse(a["published_at"])
        else:
            assert not in_quiet_hours(at)
            assert not pack.titles[a["title_id"]].injection
    per_day = {}
    for a in actions:
        if a["type"] in ("push", "whatsapp"):
            k = (a["user_id"], parse(a["send_at"]).date())
            per_day[k] = per_day.get(k, 0) + 1
    assert max(per_day.values(), default=0) <= 1
    trig = [d for d in decisions if d["kind"] == "trigger"]
    assert all(d["latency_ms"] <= 60_000 for d in trig)
    assert len({d["trigger"]["event_id"] for d in trig}) == len(trig), "one decision per trigger event_id"
    for d in trig:
        assert d["action"] != "NO_ACTION" or d["no_action_reason"], "every no-action has a recorded reason"
        if d["goal"]:
            assert d["goal"]["id"] not in ("G10", "G11")
    # Withdrawn title is never published after withdrawal.
    for a in actions:
        if parse(a["published_at"]) >= parse("2026-10-01T07:00:00Z") and a["type"] == "home_page":
            assert "T0001" not in [a["hero_title_id"]] + [t for row in a["rows"] for t in row["title_ids"]]
