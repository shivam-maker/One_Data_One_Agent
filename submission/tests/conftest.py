import copy
import sys
import uuid
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oua.agent import AgentSystem  # noqa: E402
from oua.config import Settings  # noqa: E402
from oua.events import EventStore, read_jsonl  # noqa: E402
from oua.llm import MockProvider  # noqa: E402
from oua.pack import Pack  # noqa: E402
from oua.runtime import DependencyFaults  # noqa: E402
from oua.store import StateStore  # noqa: E402
from oua.timeutil import iso, parse  # noqa: E402


@pytest.fixture(scope="session")
def settings():
    s = Settings()
    assert s.pack_dir.exists(), f"data pack not found at {s.pack_dir}"
    return s


@pytest.fixture(scope="session")
def base(settings):
    pack = Pack.load(settings.pack_dir)
    events = list(read_jsonl(settings.pack_dir / "data/events.jsonl"))
    by_user = {}
    for e in events:
        by_user.setdefault(e["user_id"], []).append(e)
    pack.compute_popularity(EventStore_from(events).all_events())
    faults = {f["trigger_event_id"]: f for f in read_jsonl(settings.pack_dir / "replay/model_faults.jsonl")}
    triggers = list(read_jsonl(settings.pack_dir / "replay/triggers.jsonl"))
    return {"pack": pack, "by_user": by_user, "faults": faults, "triggers": triggers}


def EventStore_from(events):
    s = EventStore()
    s.ingest_many(events)
    return s


@pytest.fixture
def make_system(base, settings):
    def _make(users=(), extra_events=(), faults=None, provider=None):
        pack = copy.copy(base["pack"])
        pack.withdrawn = {}
        store = EventStore()
        for u in users:
            store.ingest_many(base["by_user"].get(u, []))
        store.ingest_many(extra_events)
        prov = provider or MockProvider(base["faults"] if faults is None else faults)
        return AgentSystem(copy.copy(settings), pack, store, StateStore(":memory:"), prov, DependencyFaults())
    return _make


def make_event(user, event, at, dialect="d1", session=None, props=None, buffered_s=0, clock_offset_s=0):
    """Event with a trustworthy received_at=at. buffered_s: how long the phone held it; clock_offset_s: phone clock
    error (applied to occurred_at and sent_at equally)."""
    at = parse(at) if isinstance(at, str) else at
    occurred = at - timedelta(seconds=buffered_s) + timedelta(seconds=clock_offset_s)
    sent = at + timedelta(seconds=clock_offset_s)
    return {"event_id": str(uuid.uuid4()), "user_id": user, "event": event, "occurred_at": iso(occurred),
            "sent_at": iso(sent), "received_at": iso(at), "deliver_at": iso(at),
            "session_id": session or str(uuid.uuid4()), "app_dialect": dialect, "app_language": "hin",
            "platform": "android", "app_version": "6.1.4", "properties": props or {}}


def agent_user(base, predicate):
    pack = base["pack"]
    for u, arm in sorted(pack.arms.items()):
        if arm == "agent" and predicate(u):
            return u
    raise LookupError("no matching user")
