"""Durable per-viewer state (SQLite).

Tables
  viewer_state   one JSON document per viewer, written with an optimistic version check (single owner).
  trigger_ledger every trigger event_id ever seen -> decision_id (idempotency).
  decisions      append-only decision records (the machine-readable output of every decision).
  actions        append-only published actions (+ status: published | superseded | cancelled).
  goals          one row per goal instance (agent and holdout shadow goals), indexed by goal_id and status, so a
                 rule correction touches only the open goals of that goal_id.
  audit          everything else that changed a viewer's state: observed events, plan changes, control events.
"""
from __future__ import annotations

import functools
import json
import sqlite3
import threading
from pathlib import Path


def _locked(fn):
    """One shared connection: every statement runs under the store lock (re-entrant, so it nests inside tx())."""
    @functools.wraps(fn)
    def wrapper(self, *a, **kw):
        with self.lock:
            return fn(self, *a, **kw)
    return wrapper

SCHEMA = """
CREATE TABLE IF NOT EXISTS viewer_state (user_id TEXT PRIMARY KEY, version INTEGER NOT NULL, doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS trigger_ledger (event_id TEXT PRIMARY KEY, user_id TEXT, first_deliver_at TEXT,
    decision_id TEXT, deliveries INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS decisions (decision_id TEXT PRIMARY KEY, user_id TEXT, created_at TEXT, doc TEXT);
CREATE INDEX IF NOT EXISTS decisions_user ON decisions(user_id, created_at);
CREATE TABLE IF NOT EXISTS actions (action_id TEXT PRIMARY KEY, decision_id TEXT, user_id TEXT, type TEXT,
    published_at TEXT, status TEXT, doc TEXT);
CREATE INDEX IF NOT EXISTS actions_user ON actions(user_id);
CREATE TABLE IF NOT EXISTS goals (goal_instance_id TEXT PRIMARY KEY, user_id TEXT, goal_id TEXT, arm TEXT,
    status TEXT, doc TEXT);
CREATE INDEX IF NOT EXISTS goals_open ON goals(goal_id, status);
CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, user_id TEXT, kind TEXT, doc TEXT);
CREATE INDEX IF NOT EXISTS audit_user ON audit(user_id, at);
"""


class ConcurrentUpdate(Exception):
    pass


class StateStore:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self.lock = threading.RLock()
        self._counters = {"D": 0, "A": 0, "G": 0}
        for prefix, table, col in (("D", "decisions", "decision_id"), ("A", "actions", "action_id"),
                                   ("G", "goals", "goal_instance_id")):
            row = self.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            self._counters[prefix] = row[0]

    def next_id(self, prefix: str) -> str:
        with self.lock:
            self._counters[prefix] += 1
            return f"{prefix}-{self._counters[prefix]:05d}"

    def tx(self):
        return _Tx(self)

    # ---- viewer state --------------------------------------------------------------------------------------
    @_locked
    def load_viewer(self, user_id: str) -> tuple[dict, int]:
        row = self.db.execute("SELECT doc, version FROM viewer_state WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            return new_viewer_doc(user_id), 0
        return json.loads(row[0]), row[1]

    @_locked
    def save_viewer(self, user_id: str, doc: dict, expected_version: int) -> int:
        new_v = expected_version + 1
        if expected_version == 0:
            try:
                self.db.execute("INSERT INTO viewer_state(user_id, version, doc) VALUES (?,?,?)",
                                (user_id, new_v, json.dumps(doc, default=str)))
            except sqlite3.IntegrityError as e:
                raise ConcurrentUpdate(user_id) from e
        else:
            cur = self.db.execute("UPDATE viewer_state SET doc=?, version=? WHERE user_id=? AND version=?",
                                  (json.dumps(doc, default=str), new_v, user_id, expected_version))
            if cur.rowcount != 1:
                raise ConcurrentUpdate(user_id)
        return new_v

    @_locked
    def viewers(self) -> list[str]:
        return [r[0] for r in self.db.execute("SELECT user_id FROM viewer_state ORDER BY user_id")]

    # ---- ledger --------------------------------------------------------------------------------------------
    @_locked
    def ledger_get(self, event_id: str) -> dict | None:
        row = self.db.execute("SELECT user_id, first_deliver_at, decision_id, deliveries FROM trigger_ledger "
                              "WHERE event_id=?", (event_id,)).fetchone()
        if not row:
            return None
        return {"user_id": row[0], "first_deliver_at": row[1], "decision_id": row[2], "deliveries": row[3]}

    @_locked
    def ledger_claim(self, event_id: str, user_id: str, deliver_at: str) -> bool:
        """True if this delivery is the first for event_id (atomic)."""
        try:
            self.db.execute("INSERT INTO trigger_ledger(event_id, user_id, first_deliver_at) VALUES (?,?,?)",
                            (event_id, user_id, deliver_at))
            return True
        except sqlite3.IntegrityError:
            self.db.execute("UPDATE trigger_ledger SET deliveries = deliveries + 1 WHERE event_id=?", (event_id,))
            return False

    @_locked
    def ledger_set_decision(self, event_id: str, decision_id: str) -> None:
        self.db.execute("UPDATE trigger_ledger SET decision_id=? WHERE event_id=?", (decision_id, event_id))

    # ---- decisions / actions / goals / audit ---------------------------------------------------------------
    @_locked
    def put_decision(self, d: dict) -> None:
        self.db.execute("INSERT OR REPLACE INTO decisions VALUES (?,?,?,?)",
                        (d["decision_id"], d["viewer_id"], d["trigger"].get("received_at") if d.get("trigger") else
                         d.get("created_at"), json.dumps(d, default=str)))

    @_locked
    def get_decision(self, decision_id: str) -> dict | None:
        row = self.db.execute("SELECT doc FROM decisions WHERE decision_id=?", (decision_id,)).fetchone()
        return json.loads(row[0]) if row else None

    @_locked
    def decisions(self, user_id: str | None = None) -> list[dict]:
        q = "SELECT doc FROM decisions" + (" WHERE user_id=?" if user_id else "") + " ORDER BY decision_id"
        return [json.loads(r[0]) for r in self.db.execute(q, (user_id,) if user_id else ())]

    @_locked
    def put_action(self, a: dict) -> None:
        self.db.execute("INSERT OR REPLACE INTO actions VALUES (?,?,?,?,?,?,?)",
                        (a["action_id"], a["decision_id"], a["user_id"], a["type"], a["published_at"],
                         a.get("status", "published"), json.dumps(a, default=str)))

    @_locked
    def actions(self, user_id: str | None = None) -> list[dict]:
        q = "SELECT doc, status FROM actions" + (" WHERE user_id=?" if user_id else "") + " ORDER BY action_id"
        out = []
        for doc, status in self.db.execute(q, (user_id,) if user_id else ()):
            a = json.loads(doc)
            a["status"] = status
            out.append(a)
        return out

    @_locked
    def set_action_status(self, action_id: str, status: str) -> None:
        self.db.execute("UPDATE actions SET status=? WHERE action_id=?", (status, action_id))

    @_locked
    def put_goal(self, g: dict) -> None:
        self.db.execute("INSERT OR REPLACE INTO goals VALUES (?,?,?,?,?,?)",
                        (g["goal_instance_id"], g["user_id"], g["goal_id"], g["arm"], g["status"],
                         json.dumps(g, default=str)))

    @_locked
    def goals(self, goal_id: str | None = None, status: str | None = None, user_id: str | None = None) -> list[dict]:
        q, args = "SELECT doc FROM goals WHERE 1=1", []
        for col, v in (("goal_id", goal_id), ("status", status), ("user_id", user_id)):
            if v:
                q += f" AND {col}=?"
                args.append(v)
        return [json.loads(r[0]) for r in self.db.execute(q + " ORDER BY goal_instance_id", args)]

    @_locked
    def audit(self, at: str, user_id: str | None, kind: str, doc: dict) -> None:
        self.db.execute("INSERT INTO audit(at, user_id, kind, doc) VALUES (?,?,?,?)",
                        (at, user_id, kind, json.dumps(doc, default=str)))

    @_locked
    def audit_log(self, user_id: str | None = None, kind: str | None = None) -> list[dict]:
        q, args = "SELECT at, user_id, kind, doc FROM audit WHERE 1=1", []
        if user_id:
            q += " AND user_id=?"
            args.append(user_id)
        if kind:
            q += " AND kind=?"
            args.append(kind)
        return [{"at": a, "user_id": u, "kind": k, **json.loads(d)} for a, u, k, d in
                self.db.execute(q + " ORDER BY id", args)]


class _Tx:
    def __init__(self, s: StateStore):
        self.s = s

    def __enter__(self):
        self.s.lock.acquire()
        self.s.db.execute("BEGIN")
        return self.s

    def __exit__(self, exc_type, exc, tb):
        try:
            self.s.db.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self.s.lock.release()
        return False


def new_viewer_doc(user_id: str) -> dict:
    return {
        "viewer_id": user_id,
        "active_goal": None,       # the single active goal instance (agent-owned)
        "plan": None,              # {goal_instance_id, steps:[...]} the single active plan
        "facts_cache": None,       # last assembled facts (used when the data store is unavailable)
        "facts_cache_at": None,
        "decision_ids": [],
        "action_ids": [],
        "messages": [],            # agent-published messages {action_id, ist_day, channel}
        "observed": [],            # live events the agent observed (id, type, at)
        "goal_history": [],        # closed goal instances with verdicts
        "learned": [],             # what the agent learned about this viewer
        "sessions_acted": {},      # session_id -> decision_id (idempotency per session)
    }
