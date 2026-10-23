"""Runtime plumbing shared by the replay harness and the API: simulated clock per decision, call budgets,
dependency fault windows and the tool gateway that wraps the user data store.

The replay runs faster than real time. Each decision therefore carries a DecisionClock that adds
  measured wall time of our own code + simulated latency of every dependency call (+ queueing delay),
and published_at = deliver_at + that total, as the pack README requires.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from datetime import datetime

from .events import EventStore


def h01(*parts) -> float:
    return int(hashlib.sha256(":".join(map(str, parts)).encode()).hexdigest()[:12], 16) / 16 ** 12


class BudgetExceeded(Exception):
    pass


class DependencyError(Exception):
    def __init__(self, dependency: str, kind: str, detail: str = ""):
        super().__init__(f"{dependency} {kind} {detail}".strip())
        self.dependency, self.kind, self.detail = dependency, kind, detail


@dataclass
class DecisionClock:
    """Accumulates simulated dependency latency and counts calls against the per-decision budget."""
    max_model_calls: int
    max_tool_calls: int
    queue_ms: int = 0
    simulated_ms: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    calls: list[dict] = field(default_factory=list)
    _wall_start: float = field(default_factory=time.perf_counter)

    def spend(self, ms: int) -> None:
        self.simulated_ms += int(ms)

    def count_model(self) -> None:
        if self.model_calls >= self.max_model_calls:
            raise BudgetExceeded("model call budget exhausted")
        self.model_calls += 1

    def count_tool(self) -> None:
        if self.tool_calls >= self.max_tool_calls:
            raise BudgetExceeded("tool call budget exhausted")
        self.tool_calls += 1

    def log(self, **kw) -> None:
        self.calls.append(kw)

    def wall_ms(self) -> int:
        return int((time.perf_counter() - self._wall_start) * 1000)

    def elapsed_ms(self) -> int:
        """Processing time for this decision, excluding queueing."""
        return self.wall_ms() + self.simulated_ms


@dataclass
class FaultWindow:
    dependency: str
    kind: str  # unavailable | degraded
    start: datetime
    until: datetime
    detail: str = ""


class DependencyFaults:
    def __init__(self) -> None:
        self.windows: list[FaultWindow] = []

    def add(self, w: FaultWindow) -> None:
        self.windows.append(w)

    def state(self, dependency: str, at: datetime) -> FaultWindow | None:
        for w in self.windows:
            if w.dependency == dependency and w.start <= at < w.until:
                return w
        return None


class StoreGateway:
    """The agent's only path to the user data store. Applies budgets, timeouts and injected faults."""

    def __init__(self, store: EventStore, faults: DependencyFaults, timeout_ms: int):
        self.store, self.faults, self.timeout_ms = store, faults, timeout_ms

    def history(self, user_id: str, as_of: datetime, clock: DecisionClock, call_key: str) -> list[dict]:
        clock.count_tool()
        w = self.faults.state("user_data_store", as_of)
        if w and w.kind == "unavailable":
            clock.spend(50)
            clock.log(tool="user_data_store.history", ok=False, ms=50, error="unavailable")
            raise DependencyError("user_data_store", "unavailable", w.detail)
        if w and w.kind == "degraded":
            # p95 ~8 s and 20% timeouts (control event); deterministic per call so replays are reproducible.
            lat = int(1500 + 9000 * h01("store", call_key))
            timed_out = h01("store-to", call_key) < 0.20
            if timed_out or lat > self.timeout_ms:
                clock.spend(self.timeout_ms)
                clock.log(tool="user_data_store.history", ok=False, ms=self.timeout_ms, error="timeout")
                raise DependencyError("user_data_store", "timeout", f"read exceeded {self.timeout_ms} ms")
            clock.spend(lat)
            clock.log(tool="user_data_store.history", ok=True, ms=lat)
            return self.store.history(user_id, as_of)
        lat = int(15 + 45 * h01("store", call_key))
        clock.spend(lat)
        clock.log(tool="user_data_store.history", ok=True, ms=lat)
        return self.store.history(user_id, as_of)
