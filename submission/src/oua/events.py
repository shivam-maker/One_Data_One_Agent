"""Event normalisation and the per-user event history store.

Rules (see ARCHITECTURE.md, "Source handling"):
  * received_at is set by our servers and is the only trustworthy clock.
  * occurred_at and sent_at come from the phone. Their *difference* is measured on one clock, so it is a reliable
    estimate of how long the phone buffered the event even when the phone clock itself is wrong by hours.
  * estimated occurrence time = received_at - max(0, sent_at - occurred_at).
  * an event is identified by event_id; re-deliveries are dropped.
"""
from __future__ import annotations

import json
from bisect import insort
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator

from .timeutil import parse

CLOCK_SKEW_FLAG_S = 300


def read_jsonl(path: Path) -> Iterator[dict]:
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def normalize(e: dict) -> dict:
    """Adds _received, _true_at, _buffered_s and _clock_offset_s. Leaves the original fields untouched."""
    received = parse(e["received_at"])
    occurred = parse(e.get("occurred_at")) or received
    sent = parse(e.get("sent_at")) or occurred
    buffered = max(0.0, (sent - occurred).total_seconds())
    n = dict(e)
    n["_received"] = received
    n["_true_at"] = received if buffered == 0 else received - (sent - occurred)
    n["_buffered_s"] = buffered
    n["_clock_offset_s"] = (sent - received).total_seconds()
    n["_clock_suspect"] = abs(n["_clock_offset_s"]) > CLOCK_SKEW_FLAG_S
    return n


class EventStore:
    """Durable-in-spirit store of everything received about each user (the "user data store").

    Reads are only ever answered as of a time: an agent deciding at T can never see an event received after T.
    """

    def __init__(self) -> None:
        self._by_user: dict[str, list[tuple[datetime, str, dict]]] = defaultdict(list)
        self._ids: set[str] = set()
        self.duplicates_dropped = 0

    def ingest(self, e: dict) -> dict | None:
        """Returns the normalised event, or None when it is a duplicate delivery."""
        if e["event_id"] in self._ids:
            self.duplicates_dropped += 1
            return None
        self._ids.add(e["event_id"])
        n = normalize(e)
        insort(self._by_user[e["user_id"]], (n["_received"], e["event_id"], n))
        return n

    def ingest_many(self, events: Iterable[dict]) -> None:
        for e in events:
            self.ingest(e)

    def seen(self, event_id: str) -> bool:
        return event_id in self._ids

    def history(self, user_id: str, as_of: datetime) -> list[dict]:
        """Events received at or before as_of, ordered by estimated occurrence time."""
        rows = [n for (r, _, n) in self._by_user.get(user_id, ()) if r <= as_of]
        rows.sort(key=lambda n: n["_true_at"])
        return rows

    def all_events(self) -> Iterator[dict]:
        for rows in self._by_user.values():
            for _, _, n in rows:
                yield n

    def users(self) -> list[str]:
        return list(self._by_user)
